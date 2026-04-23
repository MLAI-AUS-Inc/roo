# 2026-04-23 Roo Content Factory Action Identity Regression

## Summary

On 2026-04-23, Roo received Slack button clicks for Content Factory actions, including `Scan Again`, but some clicks did not trigger any backend action. The visible failure mode was a no-op: Slack acknowledged the click, Roo logged the action start, and then nothing happened in MLAI or Content Factory.

The immediate symptom showed up on `skedy.io` repeat-scan prompts. The root cause was not Content Factory scanning itself. It was Roo's interactive action identity model. Some buttons were emitted without complete identity, and action handlers then fell back to remembered thread context. In reused or stale threads, that could resolve to the wrong requester, so Roo rejected the click before calling MLAI backend.

The permanent fix is a shared Content Factory action identity contract. New Roo-emitted buttons now carry explicit requester and effective identities, action handlers resolve identity from payload first, and thread context remains legacy fallback only.

## Impact

- Affected Content Factory Slack actions could silently fail before reaching MLAI backend.
- Repeat-scan prompts were the observed production failure.
- Other Content Factory actions were exposed to the same class of bug because they used the same mixed payload-plus-thread-context identity resolution pattern.
- The failure happened before MLAI backend request logging, which made the issue look like a scan/backend problem when it was actually a Roo authorization-resolution problem.

## Evidence

- Roo server logs showed `prerequisite_scan` clicks being received.
- Roo did not emit the normal `trigger_repo_scan` success or failure logs afterward.
- MLAI backend logs did not show the expected scan POST for the affected click.
- Content Factory logs did not show a new scan job for the affected click.
- The repeat-scan button payload was built without canonical requester/effective identity unless the flow was explicitly delegated.
- The action handler then resolved owner identity from thread context when payload identity was incomplete.

## Root Cause

Roo had two overlapping identity systems for Content Factory interactive actions:

1. button payload identity
2. remembered thread-context identity

That model was inconsistent:

- some new actions used `requested_by_slack_user_id` and `effective_slack_user_id`
- some actions only carried legacy `slack_user_id`
- some actions carried no usable identity at all and depended on thread context

This made correctness depend on thread state. In a stale or reused thread, Roo could resolve the current click against an older requester and fail the owner check before any backend call happened.

The core design flaw was that actions were not self-contained. Roo required thread context for correctness instead of treating it as advisory legacy fallback only.

## Fix

### Shared identity contract

Added `roo/content_factory_identity.py` as the only shared place that constructs and resolves Content Factory action identity.

Canonical new-button payload:

- `requested_by_slack_user_id`
- `effective_slack_user_id`
- action metadata such as `domain`, `channel_id`, `thread_ts`, `job_id`

Rules:

- non-delegated actions: requester == effective
- delegated actions: requester != effective
- legacy `slack_user_id` remains supported as a fallback for old buttons only

### Shared resolver

All Content Factory Slack actions now resolve identity with this precedence:

1. explicit canonical payload identity
2. legacy `slack_user_id`
3. thread context fallback only for legacy/incomplete historical messages

Safety rules:

- partial explicit identity is invalid and fails safe
- explicit payload identity is never overridden by thread context
- if identity cannot be proven safely, Roo returns a stale-action ephemeral response instead of silently doing nothing

### Handler changes

Updated Content Factory interactive handlers in `roo/main.py` to use one shared action-context resolver for:

- resume flows
- prerequisite scan/scaffold/cancel flows
- topic confirm/cancel flows
- delivery mode selection
- publish PR action
- article-system decision flows

### Builder changes

Updated Content Factory button builders in `roo/skills/executor.py` so new buttons emit canonical identity, including:

- requirements confirmation
- repeat-scan confirmation
- reconnect/resume flows
- article direction buttons
- delivery mode buttons

## Change Inventory

- `roo/content_factory_identity.py`: canonical identity payload builder, resolver, stale-action guard
- `roo/main.py`: centralized Content Factory action-context resolution and owner enforcement
- `roo/skills/executor.py`: canonical payload emission for Content Factory buttons
- `roo/tests/test_content_factory_article_flow.py`: regression coverage for canonical, delegated, legacy, and invalid payloads

## Verification

Static verification completed:

- `PYTHONPYCACHEPREFIX=/tmp/roo-pycache python3 -m py_compile roo/content_factory_identity.py roo/main.py roo/skills/executor.py roo/tests/test_content_factory_article_flow.py`
- `git diff --check`

Focused runtime test coverage added for:

- canonical non-delegated identity payloads
- delegated identity payloads
- repeat-scan buttons ignoring stale thread context
- non-requester denial on delegated actions
- partial explicit identity failing safe
- legacy `slack_user_id` fallback still working

Local pytest execution was not available in the shell used during the fix because `pytest` was not installed.

## Prevention

- New Content Factory buttons must always emit canonical requester/effective identity.
- `slack_user_id` is legacy compatibility only and must not be the primary identity for new payloads.
- Thread context must never be required for correctness.
- Any future Content Factory interactive action should use the shared identity module rather than constructing or interpreting action identity inline.
