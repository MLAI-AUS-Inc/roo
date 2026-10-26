#!/usr/bin/env bash
# Run from any directory; live modes explicitly opt into model API calls.
set -euo pipefail

repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
app_dir="$repo_dir/roo-standalone"
usage() {
  cat <<'EOF'
Usage: run_evals.sh [offline|routing|addressing|live] [arguments]

  offline     Run the offline routing pytest gates (default).
  routing     Run live LLM routing evals; forward arguments to the Python CLI.
  addressing  Run live Slack addressing evals; forward arguments to the Python CLI.
  live        Run both live suites in separate processes; no extra arguments.

Examples:
  ./tests/run_evals.sh
  ./tests/run_evals.sh routing --filter blessed --json /tmp/routing.json
  ./tests/run_evals.sh addressing --min-confidence 0.90

Uses roo-standalone/.venv/bin/python when available, otherwise python3 on PATH.
Set EVAL_PYTHON to override the interpreter. Live modes use development model
credentials from the environment or roo-standalone/.env and incur API usage charges.
EOF
}

mode="${1:-offline}"
if [[ $# -gt 0 ]]; then shift; fi
case "$mode" in
  -h|--help) usage; exit 0 ;;
  offline|routing|addressing) ;;
  live)
    if [[ $# -gt 0 ]]; then usage >&2; exit 2; fi
    ;;
  *) usage >&2; exit 2 ;;
esac

if [[ -n "${EVAL_PYTHON:-}" ]]; then
  python_bin="$EVAL_PYTHON"
elif [[ -x "$app_dir/.venv/bin/python" ]]; then
  python_bin="$app_dir/.venv/bin/python"
else
  python_bin=python3
fi
cd "$app_dir"

# Neither suite needs real Slack credentials.
export SLACK_BOT_TOKEN=test
export SLACK_SIGNING_SECRET=test
export FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au

case "$mode" in
  offline)
    export OPENAI_API_KEY=test
    exec "$python_bin" -m pytest "$repo_dir/tests/roo/test_routing_eval_gate.py" -q "$@"
    ;;
  routing)
    exec "$python_bin" "$repo_dir/tests/evals/run_routing_eval.py" --mode v2 "$@"
    ;;
  addressing)
    exec "$python_bin" "$repo_dir/tests/evals/run_addressing_eval.py" "$@"
    ;;
  live)
    status=0
    "$python_bin" "$repo_dir/tests/evals/run_routing_eval.py" --mode v2 || status=1
    "$python_bin" "$repo_dir/tests/evals/run_addressing_eval.py" || status=1
    exit "$status"
    ;;
esac
