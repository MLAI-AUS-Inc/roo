# Running evaluations

## Booking and Roo Points smoke tests

From `roo-standalone` with the application Python environment:

```bash
python ../tests/run_skill_smoke.py
python ../tests/run_skill_smoke.py --live --env-file .env --report /tmp/roo-skill-smoke.json
```

The default run makes no model calls. `--live` opts into seven real router
queries using a **development-only** OpenAI key from
`ROO_SMOKE_DEV_OPENAI_API_KEY`, `OPENAI_API_KEY`, or the specified dotenv file.
Only the model key and optional `ROUTER_MODEL` are taken from that file;
operational settings are synthetic. `--model` can explicitly override the
configured router model. Never use production tokens.

Cases cover coworking today/tomorrow/an explicit date with a time range,
requesting Roo Points, and approving an existing points request in chat.
The latter must not turn into a task approval or a fresh points request:
the current approval mechanism is a Slack reaction. Two additional offline
tests exercise reaction approval success and backend permission denial.

Routing uses the real public skill catalogue. Booking tests exercise the real
executor and date parser, then substitute booking persistence. Points tests use
a fake backend and fake Slack delivery. Only OpenAI HTTP traffic is allowed;
no real booking, points mutation, Slack message, database or migration occurs.
Reaction tests start at the internal event handler; they do not test Slack
signature verification or real backend authorization. Each model request has
a 45-second timeout and no SDK retries; the router retains its existing
tool-parse retry. Live results are nondeterministic and incur API usage.

Failures remain failures (exit status 1), including provider errors. Reports
contain synthetic queries, decisions, responses and test outcomes, never keys.
The [23 October report](evals/skill_smoke/2026-10-23.md) records observed issues.

The model tool-call/execution separation follows the
[official function-calling workflow](https://developers.openai.com/api/docs/guides/function-calling).

## Routing and addressing evaluations

From the repository root (or use the script's absolute path from anywhere):

```bash
./tests/run_evals.sh                 # Offline routing gates
./tests/run_evals.sh routing         # Live model routing evaluation
./tests/run_evals.sh addressing      # Live respond/ignore evaluation
./tests/run_evals.sh live            # Both live suites
```

Install `roo-standalone/requirements.txt` in a Python 3.11 environment first. The runner
uses `roo-standalone/.venv/bin/python` if present, or `python3` from PATH. Override with
`EVAL_PYTHON=/path/to/python`. Live modes require development model credentials
in the environment or `roo-standalone/.env`; they incur model API usage. They evaluate
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
the item was saved. From `roo-standalone`, with the Python 3.11 test environment active:

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
