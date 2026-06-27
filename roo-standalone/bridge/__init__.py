"""
Slack cross-org channel bridge.

Bridges one channel in the MLAI workspace (where Roo lives; bridge runs as a
normal bot, since Sam is admin) with one channel in the Stone & Chalk workspace
(where Sam is only a member, so the bridge connects "Beeper-style" using Sam's
own web-session credentials — no app installed in S&C).

See SLACK_BRIDGE_PLAN.md at the repo root for the full design and the honest
trade-offs of the session-token approach.
"""

__version__ = "0.1.0"
