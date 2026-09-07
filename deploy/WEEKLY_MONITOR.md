# Weekly stock-bot progress monitor

**What:** a once-a-week check that reads the live logs/state and answers one
question — is the position book on track toward the LiveTradingGate Gate 3
go-live bar (30 completed round-trips / net PF ≥ 1.2 / win ≥ 30%), or is
something wrong?

**Report-only.** It never trades, never edits config, never commits. Strategy /
whitelist / capital changes stay human-gated by design.

---

## What it checks

| Area | Signal |
|------|--------|
| Gate 3 progress | completed round-trips vs 30, net-of-commission PF, win rate, expectancy/trade |
| Throughput | round-trips/week; whether the 2026-09-07 sizing change is lifting the pace (needs a prior run to compare) |
| Sizing | largest open position notional — are the old ~$1k positions recycling to ~$600? |
| Account | drawdown from peak, kill-switch state |
| Log (last 7d) | faults (stuck loops, order rejections, cycle crashes, breaker trips) vs noise (TWS restarts, AI timeouts); rule BUYs a gate held, tallied by gate |

**Verdict**, most severe first: `NEEDS_ATTENTION` · `EDGE_FAILING` · `EDGE_WEAK`
· `THROUGHPUT_STALLED` · `EARLY` · `ON_TRACK`.

Output:
- `logs/weekly_monitor_<YYYYMMDD>.md` — full report (always written)
- `logs/weekly_monitor_state.json` — last snapshot, for week-over-week deltas
- Telegram `ops_alert` when run with `--send`

---

## Run it by hand

```
make monitor-run                                        # print report, no Telegram, no baseline write
.venv/bin/python -m stock_bot.analysis.weekly_monitor            # same + updates the baseline
.venv/bin/python -m stock_bot.analysis.weekly_monitor --send     # + Telegram
.venv/bin/python -m stock_bot.analysis.weekly_monitor --quiet    # only emit if verdict != ON_TRACK
```

Exit code is non-zero only on `NEEDS_ATTENTION` / `EDGE_FAILING`.

---

## Schedule it (macOS — launchd)

```
make install-monitor      # Monday 17:30 local, via ~/Library/LaunchAgents/com.tradebot.weeklymonitor.plist
make monitor-status       # is it loaded? + tail of the run log
make uninstall-monitor    # remove the schedule (code stays)
```

The job must run **on the machine the bot runs on** — it reads the bot's log and
state files directly. launchd runs a missed job at next wake if the Mac was
asleep at 17:30 Monday.

### Moving to a new machine

The monitor logic lives in the repo (`stock_bot/analysis/weekly_monitor.py`).
Only the schedule registration is machine-local, and it's one command:

1. clone repo, create `.venv`, put `stock_bot/.env` in place (you do this for the bot anyway)
2. `make install-monitor`

Nothing else carries over — `logs/weekly_monitor_state.json` just rebuilds from
the next run.

## Schedule it (Linux / VPS — cron)

No launchd. Add to the crontab of the user that runs the bot:

```
30 17 * * 1  /path/to/trade_bot/scripts/weekly_monitor.sh
```

The script figures out the repo root from its own location — no other paths to set.
