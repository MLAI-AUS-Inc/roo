# Repository improvement log

A short record of repository cleanup, testing, security, and delivery changes.
Each entry states what changed and how it was checked. Findings without fixes
are labelled as such.

## 2026-10-22

- Moved the coding-agent guide, Roo Code reconciliation rule, and Roo agent
  setup note into `agents/`. Kept `AGENTS.md` and
  `.roo/rules/00-mlai-reconciliation.md` as symlinks so agent discovery and Roo
  Code rule loading still work. Updated the README link.
- Reviewed `.github/workflows/deploy-admin-production.yml` (findings only; no
  workflow change): add SSH host fingerprint verification, pin the SSH action,
  constrain the dispatch secret to a single-line format, and make Public Roo
  environment updates atomic.
- Classified the routing, bridge, and Slack-to-Linear plans and routing audit
  as historical design records rather than coding-agent instructions. The
  plans now live in `agents/plans/`; the audit lives in `docs/`.
- Moved the Luma–Stripe–Xero reconciliation plan to `agents/plans/`, updated
  its references, and instructed coding agents to put new plans there.
- Removed the unused root `pyproject.toml` and `uv.lock`. They were untracked,
  had no application dependencies, and specified Python 3.12 while Roo runs on
  Python 3.11.
- Moved the historical routing probe to `scripts/`, marked its limitations,
  and updated references. Its cases remain in the current routing eval set.
- Scoped the Roo Code reconciliation rule to the reconciliation workspace and
  updated its setup instructions and file paths.
