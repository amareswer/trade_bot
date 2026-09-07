"""
Weekly stock-bot progress monitor.

Answers one question every week: is the position book on track toward the
LiveTradingGate Gate 3 go-live bar (30 completed round-trips / net PF >= 1.2 /
win >= 30%), or is something wrong?

Pure file reads — no network, no yfinance, no TWS. Safe to run any time.

Sources
  stock_bot/paper_trades.csv + stock_bot/ibkr_trades.csv  (the merged book)
  stock_bot/ibkr_state.json                               (peak equity / drawdown)
  logs/stock_bot.log                                      (last 7d errors + blocked BUYs)

Outputs
  logs/weekly_monitor_<YYYYMMDD>.md   — the rendered report (always written)
  logs/weekly_monitor_state.json      — last snapshot, for week-over-week deltas

CLI
  python -m stock_bot.analysis.weekly_monitor            # print report, update baseline
  python -m stock_bot.analysis.weekly_monitor --send     # + Telegram (ops_alert)
  python -m stock_bot.analysis.weekly_monitor --quiet    # only emit if verdict != ON_TRACK
  python -m stock_bot.analysis.weekly_monitor --no-baseline-update   # dry run

The scheduled entry point is scripts/weekly_monitor.sh (installed via `make
install-monitor`); see deploy/WEEKLY_MONITOR.md.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone

from stock_bot.analysis.paper_report import (
    _expectancy_stats,
    _pair_trades,
    read_position_book,
)

_REPO_DIR   = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_LOG_FILE   = os.path.join(_REPO_DIR, "logs", "stock_bot.log")
_STATE_FILE = os.path.join(_REPO_DIR, "logs", "weekly_monitor_state.json")
_IBKR_STATE = os.path.join(_REPO_DIR, "stock_bot", "ibkr_state.json")

# Gate 3 bar — mirrors stock_bot/analysis/accuracy_tracker._GATE3_* (imported
# live below; these are only the fallbacks if that import ever moves).
_GATE3_MIN_TRADES = 30
_GATE3_MIN_PF     = 1.2
_GATE3_MIN_WIN    = 30.0

# Verdicts, most severe first — headline is the worst that applies.
_SEVERITY = [
    "NEEDS_ATTENTION",   # log shows a real fault (not a restart blip / advisory timeout)
    "EDGE_FAILING",      # net-of-cost PF < 1.0 with a usable sample
    "EDGE_WEAK",         # net-of-cost PF < 1.2 with a larger sample
    "THROUGHPUT_STALLED",# < ~1 round-trip/week and no movement since last week
    "EARLY",             # sample still too small to read the edge
    "ON_TRACK",
]

_EARLY_N        = 10     # below this, no edge verdict — just "keep going"
_WEAK_N         = 15     # PF < 1.2 only counts as EDGE_WEAK at/above this n
_MIN_PER_WEEK   = 1.0    # round-trips/week we want to see after the 2026-09-07 sizing change


# ─────────────────────────────────────────────────────────────────────────────
# Log scan
# ─────────────────────────────────────────────────────────────────────────────

# (label, severity, compiled pattern). severity: "fault" flags NEEDS_ATTENTION,
# "info" is reported but never changes the verdict.
_LOG_BUCKETS = [
    ("TWS connection refused (Gateway/TWS down)", "info",
     re.compile(r"Connect call failed|API port on TWS/IBG is open|API connection failed")),
    ("TWS connectivity lost mid-session",         "info",
     re.compile(r"Connectivity between IBKR and Trader Workstation has been lost")),
    ("AI provider timeout (advisory only)",       "info",
     re.compile(r"(nvidia_nim|mistral).*(APITimeoutError|timed out)", re.I)),
    ("AI health alert (3 failed cycles)",         "info",
     re.compile(r"AI (provider )?(health|degraded|unavailable)", re.I)),
    ("Stuck loop detected",                       "fault",
     re.compile(r"STUCK LOOP", re.I)),
    ("Order rejected",                            "fault",
     re.compile(r"Order rejected|order was rejected", re.I)),
    ("Cycle failed / unhandled exception",        "fault",
     re.compile(r"cycle \d+ failed|Traceback \(most recent call last\)|Unhandled", re.I)),
    ("Breaker tripped (kill-switch / drawdown-halt)", "fault",
     re.compile(r"KILL SWITCH active|Drawdown halt", re.I)),
]

_BLOCKED_BUY_RE = re.compile(
    r"(CORRELATION GATE|MACRO BLACKOUT|VIX crisis|VIX_CRISIS|MAX_EXPOSURE|max exposure"
    r"|MAX_POSITIONS|max \d+ positions|SIZE_SKIP|REGIME_SKIP|TSX_BLOCKED|EARNINGS)",
    re.I,
)
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})")


def _scan_log(days: int = 7, log_file: str = _LOG_FILE) -> dict:
    """Bucketed error counts + blocked-BUY tallies over the last `days`."""
    out = {
        "faults": {},           # label -> count   (drives NEEDS_ATTENTION)
        "info": {},             # label -> count   (reported only)
        "blocked_buys": {},     # gate -> count
        "lines_scanned": 0,
        "window_days": days,
        "available": os.path.exists(log_file),
    }
    if not out["available"]:
        return out

    cutoff = datetime.now().timestamp() - days * 86400
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                m = _TS_RE.match(line)
                if m:
                    try:
                        ts = datetime.strptime(
                            f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S"
                        ).timestamp()
                        if ts < cutoff:
                            continue
                    except ValueError:
                        pass
                out["lines_scanned"] += 1
                for label, sev, pat in _LOG_BUCKETS:
                    if pat.search(line):
                        key = "faults" if sev == "fault" else "info"
                        out[key][label] = out[key].get(label, 0) + 1
                bm = _BLOCKED_BUY_RE.search(line)
                if bm:
                    g = bm.group(1).upper().replace(" ", "_")
                    # normalise a few spellings
                    g = {
                        "MAX_EXPOSURE": "MAX_EXPOSURE", "MAX_2_POSITIONS": "MAX_POSITIONS",
                        "VIX_CRISIS": "VIX_CRISIS",
                    }.get(g, g)
                    out["blocked_buys"][g] = out["blocked_buys"].get(g, 0) + 1
    except OSError:
        out["available"] = False
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot
# ─────────────────────────────────────────────────────────────────────────────

def _gate3_status() -> dict:
    """Canonical gate status from LiveTradingGate (gross-PnL basis — the
    same computation that code-gates the live go-live)."""
    try:
        from stock_bot.analysis.accuracy_tracker import LiveTradingGate
        return LiveTradingGate().check_gate3()
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return {"status": "UNKNOWN", "detail": f"gate check failed: {exc}", "pairs": 0}


def _drawdown() -> dict:
    try:
        with open(_IBKR_STATE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    peak = float(s.get("peak_equity", 0.0) or 0.0)
    day_open = float(s.get("day_open_equity", 0.0) or 0.0)
    cash = float(s.get("cash", 0.0) or 0.0)
    start = float(s.get("starting_cash", 0.0) or 0.0)
    return {
        "peak_equity": peak,
        "day_open_equity": day_open,
        "cash": cash,
        "starting_cash": start,
        "realized_pnl": float(s.get("realized_pnl", 0.0) or 0.0),
        "kill_switch_tripped": bool(s.get("kill_switch_tripped", False)),
        # drawdown from peak measured on day_open (no TWS call for live equity here)
        "drawdown_from_peak_pct": (
            (peak - day_open) / peak * 100 if peak > 0 and day_open > 0 else None
        ),
    }


def snapshot() -> dict:
    trades = read_position_book()
    pairs, open_pos = _pair_trades(trades)
    stats = _expectancy_stats(pairs) or {}
    g3 = _gate3_status()
    dd = _drawdown()
    log = _scan_log()

    # per-position notional (entry basis) — is the 2026-09-07 sizing change
    # actually shrinking new positions toward ~$600?
    pos_notional = {
        sym: round(p["shares"] * p["avg_cost"], 2) for sym, p in open_pos.items()
    }

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "gate3": {
            "status": g3.get("status"),
            "detail": g3.get("detail"),
            "pairs": g3.get("pairs", len(pairs)),
            "pf_gross": g3.get("pf"),
            "win_pct": g3.get("win_pct"),
        },
        "net_of_cost": {
            "n": stats.get("n", 0),
            "net_pf": stats.get("net_pf"),
            "net_win_rate": stats.get("net_win_rate"),
            "expectancy_usd": stats.get("expectancy_usd"),
            "trades_per_week": stats.get("trades_per_week"),
        },
        "open_positions": {
            "count": len(open_pos),
            "notional": pos_notional,
            "max_notional": max(pos_notional.values()) if pos_notional else None,
            "avg_notional": (
                round(sum(pos_notional.values()) / len(pos_notional), 2)
                if pos_notional else None
            ),
        },
        "account": dd,
        "log": log,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Verdict
# ─────────────────────────────────────────────────────────────────────────────

def _worst(verdicts: list[str]) -> str:
    for v in _SEVERITY:
        if v in verdicts:
            return v
    return "ON_TRACK"


def verdict(cur: dict, prev: dict | None) -> tuple[str, list[str]]:
    """Return (headline_verdict, reasons)."""
    reasons: list[str] = []
    flags: list[str] = []

    n = cur["net_of_cost"]["n"]
    net_pf = cur["net_of_cost"]["net_pf"]
    per_week = cur["net_of_cost"]["trades_per_week"]

    # ── faults from the log ──
    faults = cur["log"]["faults"]
    if faults:
        flags.append("NEEDS_ATTENTION")
        for label, c in sorted(faults.items(), key=lambda kv: -kv[1]):
            reasons.append(f"log: {label} ×{c} in the last {cur['log']['window_days']}d")
    if cur["account"].get("kill_switch_tripped"):
        flags.append("NEEDS_ATTENTION")
        reasons.append("account: kill-switch is TRIPPED — no new BUYs until cleared")

    dd = cur["account"].get("drawdown_from_peak_pct")
    if dd is not None and dd >= 5.0:
        flags.append("NEEDS_ATTENTION")
        reasons.append(f"account: drawdown {dd:.1f}% from peak (>= 5% — capital rule breach)")

    # ── edge ──
    if n < _EARLY_N:
        flags.append("EARLY")
        reasons.append(
            f"edge: only {n}/{_GATE3_MIN_TRADES} round-trips — sample too small to read "
            f"(need >= {_EARLY_N} for a first PF signal)"
        )
    elif net_pf is not None and net_pf < 1.0:
        flags.append("EDGE_FAILING")
        reasons.append(
            f"edge: net-of-cost PF {net_pf:.2f} < 1.0 over {n} round-trips — "
            f"the strategy is losing money live after fees"
        )
    elif net_pf is not None and net_pf < _GATE3_MIN_PF and n >= _WEAK_N:
        flags.append("EDGE_WEAK")
        reasons.append(
            f"edge: net-of-cost PF {net_pf:.2f} < {_GATE3_MIN_PF} over {n} round-trips — "
            f"below the go-live bar; watch whether it firms up or keeps sliding"
        )
    else:
        if net_pf is not None:
            reasons.append(
                f"edge: net-of-cost PF {net_pf:.2f} over {n} round-trips "
                f"(bar: >= {_GATE3_MIN_PF} at n>=30)"
            )

    # ── throughput ──
    # Only a verdict once there's a prior run to compare against AND the sample
    # is past EARLY — pace over <10 round-trips or a single reading is noise.
    if per_week is not None and per_week < _MIN_PER_WEEK and prev is not None and n >= _EARLY_N:
        dn = n - prev.get("net_of_cost", {}).get("n", n)
        if dn >= 2:
            reasons.append(
                f"throughput: long-run {per_week:.1f}/week but {dn} new round-trips "
                f"this week — pace may be picking up"
            )
        else:
            flags.append("THROUGHPUT_STALLED")
            reasons.append(
                f"throughput: {per_week:.1f} round-trips/week (< {_MIN_PER_WEEK:.0f}) "
                f"and {dn} new this week — the 2026-09-07 sizing change is not lifting pace yet"
            )
    elif per_week is not None:
        _ctx = " (no prior run yet — trend starts next week)" if prev is None else ""
        reasons.append(f"throughput: {per_week:.1f} round-trips/week{_ctx}")

    # ── sizing-change check ──
    mx = cur["open_positions"]["max_notional"]
    if mx is not None and prev is not None:
        pmx = prev.get("open_positions", {}).get("max_notional")
        if pmx and mx > 850 and mx >= pmx - 1:
            reasons.append(
                f"sizing: largest open position still ${mx:,.0f} — the old fat "
                f"positions have not recycled yet (expected, ~4wk)"
            )

    # ── week-over-week context ──
    if prev is not None:
        pn = prev.get("net_of_cost", {}).get("n")
        if pn is not None:
            reasons.append(f"delta: round-trips {pn} → {n} ({n - pn:+d} since last run)")

    return _worst(flags), reasons


# ─────────────────────────────────────────────────────────────────────────────
# Render
# ─────────────────────────────────────────────────────────────────────────────

_VERDICT_LINE = {
    "ON_TRACK":           "✅ ON TRACK — nothing needs you.",
    "EARLY":              "🟡 EARLY — running fine, sample still too small to judge the edge.",
    "THROUGHPUT_STALLED": "🟠 THROUGHPUT STALLED — trades are not accumulating fast enough.",
    "EDGE_WEAK":          "🟠 EDGE WEAK — net-of-cost PF is under the go-live bar.",
    "EDGE_FAILING":       "🔴 EDGE FAILING — the strategy is losing money live after fees.",
    "NEEDS_ATTENTION":    "🔴 NEEDS ATTENTION — a fault in the logs or a breached limit.",
}


def _fmt(v, spec="", dash="—"):
    if v is None:
        return dash
    try:
        return format(v, spec)
    except (ValueError, TypeError):
        return str(v)


def render(cur: dict, prev: dict | None, head: str, reasons: list[str]) -> str:
    g3 = cur["gate3"]
    nc = cur["net_of_cost"]
    op = cur["open_positions"]
    ac = cur["account"]
    L: list[str] = []
    L.append("# Weekly Stock-Bot Monitor")
    L.append("")
    L.append(f"_{cur['generated']}_")
    L.append("")
    L.append(f"## {_VERDICT_LINE.get(head, head)}")
    L.append("")
    for r in reasons:
        L.append(f"- {r}")
    L.append("")
    L.append("## Gate 3 — go-live bar (30 round-trips / PF ≥ 1.2 / win ≥ 30%)")
    L.append("")
    L.append(f"| metric | value | bar |")
    L.append(f"|---|---|---|")
    L.append(f"| status | {g3['status']} | PASS |")
    L.append(f"| completed round-trips | {g3['pairs']} | ≥ 30 |")
    L.append(f"| PF (gross, gate basis) | {_fmt(g3['pf_gross'], '.2f')} | ≥ 1.2 |")
    L.append(f"| PF (net of commission) | {_fmt(nc['net_pf'], '.2f')} | ≥ 1.2 |")
    L.append(f"| win rate (net) | {_fmt(nc['net_win_rate'], '.0f')}% | ≥ 30% |")
    L.append(f"| expectancy / trade (net) | ${_fmt(nc['expectancy_usd'], '.2f')} | > 0 |")
    L.append(f"| pace | {_fmt(nc['trades_per_week'], '.1f')} round-trips/week | ≥ 1.0 |")
    if prev:
        pnc = prev.get("net_of_cost", {})
        L.append("")
        L.append(
            f"_Since last run: round-trips {pnc.get('n', '—')} → {nc['n']}, "
            f"net PF {_fmt(pnc.get('net_pf'), '.2f')} → {_fmt(nc['net_pf'], '.2f')}._"
        )
    L.append("")
    L.append("## Open positions")
    L.append("")
    L.append(f"- count: {op['count']}")
    if op["notional"]:
        L.append(f"- largest: ${_fmt(op['max_notional'], ',.0f')}  ·  average: ${_fmt(op['avg_notional'], ',.0f')}")
        L.append(
            "  - " + ", ".join(
                f"{s} ${v:,.0f}" for s, v in sorted(
                    op["notional"].items(), key=lambda kv: -kv[1]
                )
            )
        )
        L.append(
            "  - target after the 2026-09-07 sizing change: ~$600 "
            "(≈12% of a ~$5k book); fat positions recycle over ~4 weeks"
        )
    if ac:
        L.append("")
        L.append("## Account")
        L.append("")
        L.append(f"- starting cash: ${_fmt(ac.get('starting_cash'), ',.2f')}")
        L.append(f"- cash on hand:  ${_fmt(ac.get('cash'), ',.2f')}")
        L.append(f"- realized P&L:  ${_fmt(ac.get('realized_pnl'), ',.2f')}")
        L.append(f"- peak equity:   ${_fmt(ac.get('peak_equity'), ',.2f')}")
        L.append(f"- drawdown from peak: {_fmt(ac.get('drawdown_from_peak_pct'), '.1f')}%  (halt at 15%, kill at 20%)")
        L.append(f"- kill-switch: {'TRIPPED' if ac.get('kill_switch_tripped') else 'clear'}")

    lg = cur["log"]
    L.append("")
    L.append(f"## Log scan — last {lg['window_days']} days")
    L.append("")
    if not lg["available"]:
        L.append("- log file not found (running off-box?) — log checks skipped")
    else:
        if lg["faults"]:
            L.append("**Faults (drive NEEDS ATTENTION):**")
            for k, v in sorted(lg["faults"].items(), key=lambda kv: -kv[1]):
                L.append(f"- {k} — ×{v}")
        else:
            L.append("- no faults")
        if lg["info"]:
            L.append("")
            L.append("**Noise (reported, not a fault):**")
            for k, v in sorted(lg["info"].items(), key=lambda kv: -kv[1]):
                L.append(f"- {k} — ×{v}")
        if lg["blocked_buys"]:
            L.append("")
            L.append("**Rule BUYs a gate held:**")
            for k, v in sorted(lg["blocked_buys"].items(), key=lambda kv: -kv[1]):
                L.append(f"- {k} — ×{v}")
            if any("CORRELATION" in k for k in lg["blocked_buys"]):
                L.append(
                    "  - CORRELATION climbing after the sizing change is expected "
                    "(more open positions vs the >0.70 gate); a sustained large count "
                    "means the correlation gate is the new bottleneck"
                )
    L.append("")
    L.append("---")
    L.append(
        "_Report-only. This tool never trades, never edits config, never commits. "
        "Strategy / whitelist / capital changes stay human-gated by design._"
    )
    return "\n".join(L)


# ─────────────────────────────────────────────────────────────────────────────
# Baseline + run
# ─────────────────────────────────────────────────────────────────────────────

def _load_baseline(path: str = _STATE_FILE) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _save_baseline(snap: dict, path: str = _STATE_FILE) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def run(
    send: bool = False,
    quiet: bool = False,
    update_baseline: bool = True,
) -> dict:
    """Build the report. Returns {verdict, report, report_path, sent}."""
    prev = _load_baseline(_STATE_FILE)
    cur = snapshot()
    head, reasons = verdict(cur, prev)
    report = render(cur, prev, head, reasons)

    stamp = datetime.now().strftime("%Y%m%d")
    report_path = os.path.join(_REPO_DIR, "logs", f"weekly_monitor_{stamp}.md")
    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    except OSError:
        report_path = ""

    emit = (not quiet) or head != "ON_TRACK"

    if emit and not quiet:
        print(report)
    elif emit and quiet:
        print(f"[weekly_monitor] {head}: {reasons[0] if reasons else ''}")

    sent = False
    if send and emit:
        sent = _send_telegram(head, cur, reasons)

    if update_baseline:
        _save_baseline(cur, _STATE_FILE)

    return {
        "verdict": head,
        "report": report,
        "report_path": report_path,
        "sent": sent,
        "emitted": emit,
    }


def _send_telegram(head: str, cur: dict, reasons: list[str]) -> bool:
    try:
        from stock_bot.config import load as load_cfg
        from stock_bot.alerts.notifier import AlertNotifier
    except Exception:  # noqa: BLE001
        return False
    try:
        notifier = AlertNotifier(load_cfg())
    except Exception:  # noqa: BLE001
        return False

    g3 = cur["gate3"]
    nc = cur["net_of_cost"]
    body_lines = [
        _VERDICT_LINE.get(head, head),
        "",
        f"Round-trips: {g3['pairs']}/30   net PF: {_fmt(nc['net_pf'], '.2f')}   "
        f"pace: {_fmt(nc['trades_per_week'], '.1f')}/wk",
    ]
    body_lines += [f"• {r}" for r in reasons[:6]]
    body_lines.append("")
    body_lines.append("Full report: logs/weekly_monitor_*.md")
    try:
        notifier.ops_alert(f"Weekly monitor — {head}", "\n".join(body_lines))
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly stock-bot progress monitor")
    ap.add_argument("--send", action="store_true", help="also send a Telegram ops_alert")
    ap.add_argument("--quiet", action="store_true",
                    help="only emit output/Telegram when the verdict is not ON_TRACK")
    ap.add_argument("--no-baseline-update", action="store_true",
                    help="do not overwrite logs/weekly_monitor_state.json (dry run)")
    args = ap.parse_args(argv)

    res = run(
        send=args.send,
        quiet=args.quiet,
        update_baseline=not args.no_baseline_update,
    )
    # Exit non-zero only on a genuine fault, so a cron wrapper can alert on it.
    return 1 if res["verdict"] in ("NEEDS_ATTENTION", "EDGE_FAILING") else 0


if __name__ == "__main__":
    raise SystemExit(main())
