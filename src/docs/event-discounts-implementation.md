# Event discount implementation evidence

Recorded 20 October 2026 for `codex/roo-event-discounts`, based on Roo
commit `2c88a6db4cec3a09ab16ce7acb5386f1fb9241d3`.
The implementation is prepared for pull request review.
The isolated checkout is
`/Users/samdonegan/Documents/Code/roo-worktrees/event-discount-redemption`.

The initial relevant regression selection passed **105 tests**. Canonical event
selection and stricter quote/link validation added 13 cases; the current same
selection passes **118 tests**:

| Isolated process | Passing tests |
| --- | ---: |
| Event discounts, real skill catalogue, router and deterministic routing gate | 65 |
| Existing agent routing | 15 |
| Existing router wiring | 8 |
| Public/admin surface security | 30 |

From `src`, reproduce the selection with the fresh development
virtualenv and synthetic configuration:

```bash
roo_tests() {
  env SLACK_BOT_TOKEN=test SLACK_SIGNING_SECRET=test OPENAI_API_KEY=test \
    FOUNDER_TOOLS_LINK_ORIGINS=https://mlai.au \
    .venv/bin/python -m pytest "$@" -q
}
roo_tests roo/tests/test_event_discounts.py roo/tests/test_router_catalog.py \
  roo/tests/test_router_v2.py roo/tests/test_routing_eval_gate.py
roo_tests roo/tests/test_agent_routing.py
roo_tests roo/tests/test_agent_router_v2_wiring.py
roo_tests roo/tests/test_surface_security.py
```

Keep these processes separate: older routing tests install process-wide import
doubles that can contaminate catalogue/security tests when combined.

The tests cover private verified identity, explicit original-message consent,
workspace and thread checks, disabled issuance with retained history, stable
retry keys, pending results, unsafe links, malformed quotes and real executor
wiring. Chat's exact event-card request bypasses model routing and selects its
original canonical event ID even if extracted parameters omit or substitute it.
A returned quote must match that selection and contain valid price, balance,
event link and aware expiry/start timestamps. Coupon links are normalized to an
exact bare Luma host and one event slug with the matching coupon only.

Changed Python files compile and `git diff --check` passes. These Roo checks use
mocked service boundaries and no database or live Slack, Luma or model calls.
Backend database/PostgreSQL verification is tracked in the backend checkout.
A real Luma checkout and live Chat-to-Roo bridge acceptance still require a
designated event and approved development credentials. No production rollout or
feature enablement was performed.
