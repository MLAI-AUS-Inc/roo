# Running evaluations

From the repository root (or use the script's absolute path from anywhere):

```bash
./tests/run_evals.sh                 # Offline routing gates
./tests/run_evals.sh routing         # Live model routing evaluation
./tests/run_evals.sh addressing      # Live respond/ignore evaluation
./tests/run_evals.sh live            # Both live suites
```

Install `src/requirements.txt` in a Python 3.11 environment first. The runner
uses `src/.venv/bin/python` if present, or `python3` from PATH. Override with
`EVAL_PYTHON=/path/to/python`. Live modes require development model credentials
in the environment or `src/.env`; they incur model API usage. They evaluate
decisions without executing skills or posting Slack replies.

| Suite | Cases | Implementation |
| --- | --- | --- |
| Routing | [evals/routing/cases/](evals/routing/cases/) | [evals/routing/runner.py](evals/routing/runner.py) |
| Addressing | [evals/addressing/cases.yaml](evals/addressing/cases.yaml) | [evals/addressing/runner.py](evals/addressing/runner.py) |

The offline mode runs the same routing pytest gates used by CI: fast-path
correctness, no deterministic misroutes, and no regression against the
committed baseline. It does not measure live model quality.

Routing's live CLI reports results; its default exit status does **not** enforce
an accuracy threshold. Addressing exits unsuccessfully if any case fails. The
`live` mode runs both suites even if one fails and propagates failure status.

Pass suite-specific options after the mode:

```bash
./tests/run_evals.sh routing --filter blessed --json /tmp/roo-routing.json
./tests/run_evals.sh addressing --min-confidence 0.90
./tests/run_evals.sh --help
```

Do not overwrite `evals/routing/baseline.json` just to make a regression pass.
Review intentional behavior changes before updating the baseline through the
underlying Python CLI.
