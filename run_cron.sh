#!/usr/bin/env bash
set -euo pipefail

# Determine project directory (where this script and dwd_grib_downloader.py reside)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
cd "$SCRIPT_DIR"

# Ensure common paths are in PATH (crontab often runs with a minimal PATH)
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

# Load environment file if present (for FTP credentials, configuration, etc.)
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/.env"
    set +a
fi

# Locate Python executable (prefer virtualenv if available)
if [ -n "${PYTHON_BIN:-}" ]; then
    PYTHON="$PYTHON_BIN"
elif [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
elif [ -x "$SCRIPT_DIR/venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/venv/bin/python3"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON="$(command -v python3)"
else
    echo "Error: python3 not found." >&2
    exit 1
fi

# Prevent overlapping runs using flock
LOCK_FILE="${CRON_LOCK_FILE:-/tmp/dwd_grib_downloader.lock}"
if command -v flock >/dev/null 2>&1; then
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Another instance is already running (locked on $LOCK_FILE). Exiting."
        exit 0
    fi
fi

# Default command if no arguments are passed
if [ "$#" -eq 0 ]; then
    set -- makefinalgrib --update
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting dwd_grib_downloader.py $*"
set +e
"$PYTHON" "$SCRIPT_DIR/dwd_grib_downloader.py" "$@"
EXIT_CODE=$?
set -e
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished with exit code $EXIT_CODE"
exit $EXIT_CODE
