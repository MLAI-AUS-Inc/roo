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

## Capability honesty evals

Run `tests/roo/test_capability_honesty.py` in its own pytest process.
It covers adding an agenda item, a same-channel correction, and checking whether
the item was saved. From `src`, with the Python 3.11 test environment active:

```bash
SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au \
python -m pytest ../tests/roo/test_capability_honesty.py -q -rx
```

The three offline probes inject invented model replies and currently have strict
expected-failure markers: Roo passes those replies through without proof of an
agenda write. Use `--runxfail` to see the failing assertions. These probes test
enforcement, not whether a real model chooses to hallucinate.

To inspect actual generated replies, explicitly provision a **development-only**
OpenAI key in `ROO_CAPABILITY_EVAL_DEV_OPENAI_API_KEY` and run the same command with
`-k test_live -s`. Never use a production key. The live probes incur model API usage,
use Roo's default OpenAI chat model and public skill descriptions, and force the
no-skill fallback. They do not evaluate routing. Slack history is synthetic and
skill execution is blocked; no backend writes or Slack posts occur.

Read the printed replies: the automated checks only screen for a limitation or
non-completion disclosure and reject unverified URLs and observed unsupported
channel redirects. They are not a semantic
judge of honesty or correct handling of both sponsorship options. Provider errors
must not be interpreted as successful honesty evaluations.

The [22 October 2026 live report](evals/capability_honesty/2026-10-22.md) records the
observed replies and distinguishes unsupported advice from false write claims.
