#!/usr/bin/env python
"""
rescreen.py — monthly automated re-screen of both books (2026-07-16).

Runs the existing validation gates as subprocesses and compares their
PASS lists against the live whitelists:

  1. screen_universe.py   — Kraken CAD auto-discovery + 3-window walk-forward
                            (crypto). Auto-discovery EXCLUDES already-decided
                            bases (BTC/SOL/XRP/ETH/DOGE), so live-symbol edge
                            decay is checked by a second explicit-symbol run
                            of the same gate (SCREEN_SYMBOLS=<whitelist>,
                            liquidity gate off). Before 2026-09-01 the decay
                            check compared the discovery PASS list against the
                            whitelist directly, which flagged every live CAD
                            symbol as "decayed" every month — it was never
                            re-tested at all. (The Telegram push for this was
                            also dead until 2026-08-24, so the false flag only
                            became visible on the 2026-09-01 run.)
  2. screen_universe.py   — same gate, Kraken USD auto-discovery
     (SCREEN_QUOTE=USD)    (added 2026-08-24 — closes a real automation gap:
                            CLAUDE.md had long claimed USD re-screening was
                            "automated monthly via rescreen.py" when the code
                            never actually passed SCREEN_QUOTE=USD anywhere —
                            the USD side was manual-only since 2026-07-16.
                            No UNIVERSE_WHITELIST entry is USD today, so this
                            leg only ever surfaces NEW QUALIFIERS, never
                            decay — see _crypto_usd_whitelist() below.)
  3. stock_backtest.py    — 4-window daily walk-forward over the stock
                            WATCHLIST (re-validates every RULE_WHITELIST
                            symbol; catches decayed edges like UBER's)

Output: logs/rescreen_<date>.md + Telegram alert when anything needs
attention. THIS SCRIPT NEVER CHANGES A WHITELIST — additions and removals
stay manual, per the Validation Discipline in CLAUDE.md. It exists so the
evidence refreshes itself; the decision remains a human checkpoint. This
applies identically to the USD leg: a USD PASS is flagged for a human to
look at, never auto-added to UNIVERSE_WHITELIST or the USD Expansion
preconditions list.

Scheduled by the crypto bot's in-bot audit scheduler (monthly, 1st of the
month at RESCREEN_AUDIT_TIME, catch-up if the bot was down — see
_scheduled_audits_loop in bot/main.py). Manual run: .venv/bin/python rescreen.py

Load/runtime, measured 2026-08-24 (see CLAUDE_HISTORY.md for the full
investigation before this leg was added): the USD leg costs the same 2
Kraken API calls as the CAD leg (load_markets + fetch_tickers, negligible)
plus up to SCREEN_MAX_CANDIDATES (15 by default) Binance OHLCV fetches for
the walk-forward step — measured at ~28s/candidate, so up to ~7 extra
minutes added to the monthly job (well under this script's own 2400s
per-leg subprocess timeout, and this runs once a month off the trading
tick loop, not in it).

Env:
  RESCREEN_SKIP_CRYPTO=true   skip the CAD crypto screen (e.g. while HALT engaged)
  RESCREEN_SKIP_USD=true      skip the USD crypto screen
  RESCREEN_SKIP_STOCKS=true   skip the stock walk-forward
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import cfg  # loads .env

_SUBPROCESS_TIMEOUT_S = 2400  # per gate script


def _run_gate(script: str, extra_env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a gate script, return (returncode, combined output)."""
    env = {**os.environ, **(extra_env or {})}
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(_PROJECT_ROOT, script)],
            cwd=_PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
        return proc.returncode, proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        return -1, f"{script} timed out after {_SUBPROCESS_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001 — report, don't crash the audit
        return -1, f"{script} failed to launch: {exc}"


def _parse_pass_list(output: str) -> list[str]:
    """Extract symbols from the 'PASS (n): A, B, C' summary line both gate
    scripts print. Returns [] when nothing passed or the line is absent."""
    m = re.search(r"PASS \(\d+\): ([^\n]+)", output)
    if not m:
        return []
    return [s.strip().upper() for s in m.group(1).split(",") if s.strip()]


def _crypto_whitelist() -> set[str]:
    return {
        s.strip().upper()
        for s in (cfg.universe.universe_whitelist or "").split(",")
        if s.strip()
    }


def _crypto_usd_whitelist() -> set[str]:
    """USD-quoted subset of UNIVERSE_WHITELIST — empty today, since nothing
    USD is live-whitelisted yet (see CLAUDE.md 'USD Expansion — Preconditions
    for any USD pair promotion'). Added 2026-08-24 alongside the new USD
    screening leg: with an empty whitelist, every USD PASS surfaces as a
    NEW QUALIFIER, never a decay — there's nothing live to decay from — and
    that's the correct, useful signal (a candidate worth a human look), not
    a bug. If a USD pair is ever manually promoted to UNIVERSE_WHITELIST,
    this starts tracking its decay the same way _crypto_whitelist() already
    does for CAD, with no further code change needed."""
    return {
        s.strip().upper()
        for s in (cfg.universe.universe_whitelist or "").split(",")
        if s.strip() and s.strip().upper().endswith("/USD")
    }


def _stock_whitelist() -> set[str]:
    from stock_bot.config import load as load_stock_config
    return {
        s.strip().upper()
        for s in (load_stock_config().rule_whitelist_str or "").split(",")
        if s.strip()
    }


def _alert(lines: list[str]) -> None:
    """Fire-and-forget Telegram summary; silent no-op when unconfigured.

    2026-08-24: fixed a live bug found while adding the USD leg — this read
    cfg.telegram_bot_token/telegram_chat_id/telegram_enabled directly, but
    those fields live under cfg.alerts.* (AlertConfig), not flat on
    AppConfig. Every real attention-worthy rescreen result (edge decay, new
    qualifiers — exactly the runs where alerting matters most) has been
    silently raising AttributeError here since the config was reorganized
    into nested dataclasses, caught by this function's own try/except and
    reduced to a console-only 'Telegram alert failed' line that nothing
    reads, since this runs as an unattended monthly subprocess. The monthly
    markdown report was never affected — only the Telegram push was silently
    dead. See CLAUDE_HISTORY.md for the full trail.
    """
    try:
        from bot.alerts.telegram import TelegramAlerter
        alerter = TelegramAlerter(
            cfg.alerts.telegram_bot_token, cfg.alerts.telegram_chat_id,
            cfg.alerts.telegram_enabled,
        )
        # message(), not error() — this is a routine informational digest, not
        # a fault. error() prepends "⚠️ BOT ERROR", which made every monthly
        # run look like an incident. message() has no timestamp of its own, so
        # stamp it here.
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        alerter.message(
            "📋 Monthly re-screen:\n" + "\n".join(lines) + f"\n{stamp}"
        )
        # _send_async uses a daemon thread — give it a moment before exit
        import time
        time.sleep(5)
    except Exception as exc:  # noqa: BLE001
        print(f"  (Telegram alert failed: {exc})")


def run() -> int:
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    report_lines: list[str] = [
        f"# Monthly re-screen — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}",
        "",
        "Automated evidence refresh. Whitelists are NEVER changed by this "
        "script — see Validation Discipline in CLAUDE.md.",
        "",
    ]
    attention: list[str] = []

    # 5th field `revalidate`: when true, the gate's own auto-discovery can't
    # re-test a whitelisted symbol, so edge decay must come from a separate
    # explicit-symbol run (see the loop body). screen_universe.py's Kraken
    # discovery excludes already-decided bases (BTC/SOL/XRP/ETH/DOGE via
    # _ALWAYS_EXCLUDE_BASES), so without this every live CAD symbol was
    # flagged "edge decay" every month. stock_backtest.py re-runs the full
    # WATCHLIST (a superset of RULE_WHITELIST), so its auto-run tests every
    # whitelisted symbol directly — no separate pass needed.
    sections: list[tuple[str, str, set[str], dict[str, str] | None, bool]] = []
    if os.getenv("RESCREEN_SKIP_CRYPTO", "").lower() != "true":
        sections.append(("crypto", "screen_universe.py", _crypto_whitelist(), None, True))
    if os.getenv("RESCREEN_SKIP_USD", "").lower() != "true":
        sections.append((
            "crypto-usd", "screen_universe.py", _crypto_usd_whitelist(),
            {"SCREEN_QUOTE": "USD"}, False,
        ))
    if os.getenv("RESCREEN_SKIP_STOCKS", "").lower() != "true":
        sections.append(("stocks", "stock_backtest.py", _stock_whitelist(), None, False))

    for label, script, whitelist, extra_env, revalidate in sections:
        print(f"\n── {label}: {script} ──", flush=True)
        rc, output = _run_gate(script, extra_env=extra_env)
        passes = set(_parse_pass_list(output))
        report_lines.append(f"## {label} ({script})")
        if rc != 0:
            report_lines.append(f"- ⚠ gate script exited rc={rc} — result unusable")
            attention.append(f"⚠ {label} re-screen FAILED to run (rc={rc})")
            report_lines.append("")
            continue

        report_lines.append(f"- PASS: {', '.join(sorted(passes)) or '(none)'}")
        report_lines.append(f"- whitelist now: {', '.join(sorted(whitelist)) or '(none)'}")

        # New qualifiers always come from the auto-discovery run above.
        newly = sorted(passes - whitelist)

        # Edge decay: for `revalidate` gates, run the gate again with an
        # explicit SCREEN_SYMBOLS list (liquidity gate off — a live symbol's
        # liquidity isn't the question) and derive decay from that alone.
        revalidate_note: str | None = None
        if revalidate and whitelist:
            print(f"  re-validating whitelist: {', '.join(sorted(whitelist))}", flush=True)
            rc2, output2 = _run_gate(script, extra_env={
                **(extra_env or {}),
                "SCREEN_SYMBOLS": ",".join(sorted(whitelist)),
                "SCREEN_MIN_VOL_CAD": "0",
            })
            output = output + "\n\n─── whitelist re-validation ───\n" + output2
            if rc2 == 0:
                decayed = sorted(whitelist - set(_parse_pass_list(output2)))
            else:
                decayed = []
                revalidate_note = f"whitelist re-validation FAILED to run (rc={rc2}) — decay not checked"
        else:
            decayed = sorted(whitelist - passes)

        if decayed:
            report_lines.append(f"- 🔻 EDGE DECAY — whitelisted but failed re-validation: {', '.join(decayed)}")
            attention.append(f"🔻 {label} edge decay: {', '.join(decayed)}")
        if newly:
            report_lines.append(f"- 🆕 NEW QUALIFIERS — passed but not whitelisted: {', '.join(newly)}")
            attention.append(f"🆕 {label} new qualifiers: {', '.join(newly)}")
        if revalidate_note:
            report_lines.append(f"- ⚠ {revalidate_note}")
            attention.append(f"⚠ {label} {revalidate_note}")
        if not decayed and not newly and not revalidate_note:
            report_lines.append("- ✓ no changes — whitelist matches the evidence")
        report_lines.append("")

        # Keep the gate's own summary table for forensics
        tail = "\n".join(output.strip().splitlines()[-25:])
        report_lines.append("<details><summary>gate output (tail)</summary>")
        report_lines.append("")
        report_lines.append("```")
        report_lines.append(tail)
        report_lines.append("```")
        report_lines.append("</details>")
        report_lines.append("")

    out_path = Path(_PROJECT_ROOT) / "logs" / f"rescreen_{today}.md"
    out_path.write_text("\n".join(report_lines), encoding="utf-8")
    print(f"\n  Report → {out_path}")

    if attention:
        print("\n  ATTENTION:")
        for line in attention:
            print(f"    {line}")
        _alert(attention + [f"Report: logs/rescreen_{today}.md"])
    else:
        print("\n  No changes — whitelists match the evidence.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
