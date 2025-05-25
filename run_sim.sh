#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# 1) bring in .env
if [[ -f .env ]]; then
  export $(grep -E -v '^\s*#' .env | xargs -d '\n')
fi


# 2) now *override* whatever .env set:

export API_KEY=DcYB+RUtounx1Wlz5F7oMzI2Gi3+OdwGVWcVq/NyamWCS9RDTgqtsvVY
export API_SECRET=TVKhSks0n0GEIEsI8A9RhJoLkR10sB/wxx+Dh57jeIldLrS1LIb5G7eeYwpHojL7yja83kWCqrrNqXEGyCa7XFYP
export MODE=SIM

source venv/bin/activate
python3 -u kraken-bot.py
