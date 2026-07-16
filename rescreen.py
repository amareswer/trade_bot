#!/usr/bin/env python
"""
rescreen.py — monthly automated re-screen of both books (2026-07-16).

Runs the two existing validation gates as subprocesses and compares their
PASS lists against the live whitelists:

  1. screen_universe.py   — Kraken CAD auto-discovery + 3-window walk-forward
                            (crypto; includes BTC/CAD, so live-symbol edge
                            decay is caught too)
  2. stock_backtest.py    — 4-window daily walk-forward over the stock
                            WATCHLIST (re-validates every RULE_WHITELIST
                            symbol; catches decayed edges like UBER's)

Output: logs/rescreen_<date>.md + Telegram alert when anything needs
attention. THIS SCRIPT NEVER CHANGES A WHITELIST — additions and removals
stay manual, per the Validation Discipline in CLAUDE.md. It exists so the
evidence refreshes itself; the decision remains a human checkpoint.

Scheduled by the crypto bot's in-bot audit scheduler (monthly, 1st of the
month at RESCREEN_AUDIT_TIME, catch-up if the bot was down — see
_scheduled_audits_loop in bot/main.py). Manual run: .venv/bin/python rescreen.py

Env:
  RESCREEN_SKIP_CRYPTO=true   skip the crypto screen (e.g. while HALT engaged)
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


def _stock_whitelist() -> set[str]:
    from stock_bot.config import load as load_stock_config
    return {
        s.strip().upper()
        for s in (load_stock_config().rule_whitelist_str or "").split(",")
        if s.strip()
    }


def _alert(lines: list[str]) -> None:
    """Fire-and-forget Telegram summary; silent no-op when unconfigured."""
    try:
        from bot.alerts.telegram import TelegramAlerter
        alerter = TelegramAlerter(
            cfg.telegram_bot_token, cfg.telegram_chat_id, cfg.telegram_enabled
        )
        alerter.error("📋 Monthly re-screen:\n" + "\n".join(lines))
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

    sections = []
    if os.getenv("RESCREEN_SKIP_CRYPTO", "").lower() != "true":
        sections.append(("crypto", "screen_universe.py", _crypto_whitelist()))
    if os.getenv("RESCREEN_SKIP_STOCKS", "").lower() != "true":
        sections.append(("stocks", "stock_backtest.py", _stock_whitelist()))

    for label, script, whitelist in sections:
        print(f"\n── {label}: {script} ──", flush=True)
        rc, output = _run_gate(script)
        passes = set(_parse_pass_list(output))
        report_lines.append(f"## {label} ({script})")
        if rc != 0:
            report_lines.append(f"- ⚠ gate script exited rc={rc} — result unusable")
            attention.append(f"⚠ {label} re-screen FAILED to run (rc={rc})")
            report_lines.append("")
            continue

        report_lines.append(f"- PASS: {', '.join(sorted(passes)) or '(none)'}")
        report_lines.append(f"- whitelist now: {', '.join(sorted(whitelist)) or '(none)'}")

        decayed = sorted(whitelist - passes)
        newly   = sorted(passes - whitelist)
        if decayed:
            report_lines.append(f"- 🔻 EDGE DECAY — whitelisted but failed re-validation: {', '.join(decayed)}")
            attention.append(f"🔻 {label} edge decay: {', '.join(decayed)}")
        if newly:
            report_lines.append(f"- 🆕 NEW QUALIFIERS — passed but not whitelisted: {', '.join(newly)}")
            attention.append(f"🆕 {label} new qualifiers: {', '.join(newly)}")
        if not decayed and not newly:
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
