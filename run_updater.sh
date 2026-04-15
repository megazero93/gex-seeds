#!/bin/bash
# run_updater.sh — Generate GEX seed CSVs and push to GitHub
#
# SETUP (one-time):
#   1. git init && git remote add origin https://github.com/YOU/gex-seeds.git
#   2. chmod +x run_updater.sh
#   3. Add to crontab (crontab -e):
#        */5 13-21 * * 1-5 /Users/garychang/tradingview/run_updater.sh >> /tmp/gex_updater.log 2>&1
#      (13:00–21:00 UTC = 9:00 AM–5:00 PM ET during EDT)
#
# REQUIRES: ThetaData Terminal running on localhost:25503
# OPTIONAL CLI args are forwarded to generate_gex.py as symbol overrides:
#   ./run_updater.sh SPY QQQ

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== GEX update $(date -u '+%Y-%m-%d %H:%M:%S UTC') ==="

# Activate virtualenv if present
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
elif [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# Run generator — pass any CLI args through as symbol overrides
python generate_gex.py "$@"

# Only commit + push if CSV files actually changed
if git diff --quiet -- data/; then
    echo "No data changes — skipping push."
else
    git add data/
    git commit -m "GEX update $(date -u '+%Y-%m-%d %H:%M UTC')"
    git push
    echo "Pushed to GitHub."
fi
