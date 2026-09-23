#!/usr/bin/env bash
# One-shot setup + the two genuine LLM discovery runs.
# Usage (from the repo root, in a normal terminal):   bash scripts/discover_all.sh
# Prompts for GEMINI_API_KEY (or ANTHROPIC_API_KEY) if not set; the key is never written to disk.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
"$PY" -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ required"' || { echo "Need Python 3.10+ (set PYTHON=/path/to/python3.11)"; exit 1; }

if [ ! -d .venv ]; then "$PY" -m venv .venv; fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
python -m playwright install chromium
[ -f .env ] || cp .env.example .env

if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  read -r -s -p "Paste your Gemini API key (input hidden): " GEMINI_API_KEY; echo
  export GEMINI_API_KEY
fi

python -m mockapp.server > /tmp/corelink-mock.log 2>&1 &
MOCK=$!
trap 'kill $MOCK 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8600/ && break; sleep 0.3; done

echo "== Discovery 1: member savings balance"
python -m cua discover --tenant acme \
  --capability-id member.get_savings_balance \
  --goal "Look up member 100234 and read the current balance of their primary savings account" \
  --param member_id=100234:integer:pii | tee /tmp/discovery1.json

echo "== Discovery 2: open a new share (sub-account) up to the review screen"
python -m cua discover --tenant acme \
  --capability-id share.open_account_to_review \
  --goal "Start opening a new HOLIDAY CLUB share account for member 100234 with an initial deposit of 25.00, and stop on the review/confirmation screen without confirming" \
  --param member_id=100234:integer:pii \
  --param "share_type=HOLIDAY CLUB:string" \
  --param initial_deposit=25.00:decimal | tee /tmp/discovery2.json

echo
echo "Done. Evidence is in evidence/runs/discovery-*; capabilities in capabilities/."
echo "Tell Claude it finished."
