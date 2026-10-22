# Public Roo mentions from MLAI Chat

The Chat bridge posts to Slack using its bot identity. For bot-origin
`app_mention` events, Public Roo resolves the exact workspace/channel/message/
thread through the backend's authenticated
`GET /api/v1/integrations/bridge/roo/actor` endpoint before running the agent.
The backend returns the active account-backed Slack author and the stored
source text with explicit mentions rendered. A visible “Name (MLAI Chat)”
prefix never authorizes an action.

Roo uses the returned human actor for its ordinary role/points checks and
retains the original Slack reply destination. The source event ID becomes the
stable actor-context event ID. Human Slack mentions keep their existing route;
Admin Roo does not accept delegated bot mentions. Private Chat mirrors are not
supported by this public bridge endpoint.

Bot app mentions use the same durable Slack receipt lease as human mentions.
Backend `409 bridge_delivery_pending` responses receive three short retries
(0.2, 0.4, 0.8 seconds): the Slack callback can beat the posting worker's link
commit. If resolution remains pending, the backend is unavailable, the reply
scope is invalid, or service credentials are misconfigured, the task fails
before agent execution and releases the receipt for Slack retry. Explicitly
unauthorized delegation is ignored. No fallback runs as the bridge bot.

Deploy the backend endpoint first, then Public Roo. The existing `ROO_API_KEY`
must match the backend's strict Roo credential; an unrelated internal API key
cannot authorize the lookup. No extra Slack scopes or database migrations are
needed. This change does not replay past requests or award points on deployment.

Validation: `roo/tests/test_bridge_mentions.py`, `test_slack_security.py`,
`test_internal_mentions.py`, `test_meeting_room_clarifications.py`, and
`test_points_requests.py`. Use synthetic credentials and stub external calls.

Desktop, browser, iOS and Android device keys resolve through their existing verified
MLAI account to the same Slack member and therefore Roo points owner. Neither a
matching email address nor the message's visible name grants account authority.
The tests cover all three device keys, revoked devices and the booking command
reaching Roo's normal agent with the member's identity; they do not make bookings
or spend points.

The actor-resolution change must be merged into each repository's release branch
before deployment. A temporary branch deployment can otherwise be overwritten by
the next main release. The backend endpoint must be present before updating Roo.
