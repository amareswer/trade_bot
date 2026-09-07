#!/usr/bin/env bash
# Weekly stock-bot progress monitor — scheduled entry point.
#
# Runs stock_bot.analysis.weekly_monitor against the live logs/state on THIS
# machine (it reads the bot's files, so it must run where the bot runs) and
# sends a Telegram summary. Report-only: never trades, edits config, or commits.
#
# Install / remove the schedule with:  make install-monitor  /  make uninstall-monitor
# Details: deploy/WEEKLY_MONITOR.md
set -euo pipefail

# Repo root = parent of this script's dir. Works regardless of where cron/launchd
# invokes it from, and survives a machine move (no absolute paths baked in).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_DIR}"

PY="${REPO_DIR}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
    echo "weekly_monitor: ${PY} not found — create the venv (see CLAUDE.md)" >&2
    exit 127
fi

mkdir -p "${REPO_DIR}/logs"
LOG="${REPO_DIR}/logs/weekly_monitor_cron.log"

echo "── $(date '+%Y-%m-%d %H:%M:%S %Z') — weekly_monitor run ──" >> "${LOG}"
# --send: Telegram summary. No --quiet: always write the full report file.
# Exit is non-zero only on NEEDS_ATTENTION / EDGE_FAILING.
set +e
"${PY}" -m stock_bot.analysis.weekly_monitor --send >> "${LOG}" 2>&1
rc=$?
set -e
echo "── exit ${rc} ──" >> "${LOG}"
exit ${rc}
