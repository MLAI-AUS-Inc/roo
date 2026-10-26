# Tests and evaluations

- `roo/`: application unit, integration, security, and deployment tests.
- `bridge/`: Slack bridge tests.
- `evals/routing/`: routing cases, runner, baseline, and historical reports.
- `evals/addressing/`: respond/ignore cases and runner.
- `run_evals.sh`: entry point for offline and live evaluations; see [EVALS.md](EVALS.md).

Use Python 3.11 with `src/requirements.txt` installed. From the repository root:

```bash
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au \
python -m pytest tests/roo/test_routing_eval_gate.py -q

./tests/run_evals.sh offline
```

The root `pytest.ini` adds `src/` and the repository root to the import path.
Tests can also run from `src/` using `../tests/...` paths.

Some legacy modules install process-wide import stubs. Keep the groups in
`.github/workflows/deploy.yml` separate when reproducing CI; a single combined
pytest invocation is not equivalent. Live evals require an explicit mode and
development model credentials; normal offline gates do not call the model.
