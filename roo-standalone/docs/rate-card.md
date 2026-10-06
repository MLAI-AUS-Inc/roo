# Roo rate-card outcomes

Roo reads standard volunteer point rates from
`GET /api/v1/points/rate-card/`. See the
[backend rate-card contract](https://github.com/MLAI-AUS-Inc/mlai-backend/blob/main/docs/roo-rate-card.md)
for authentication and response fields.

## Credentials and client contract

The client uses Public Roo's service headers (`X-API-Key`), not its admin
headers. Roo prefers `ROO_API_KEY`; the backend's existing read permission
also supports internal and legacy MLAI read credentials and authenticated
browser users. Public and Admin Roo configuration stay separate.

`MLAIBackendClient.get_rate_card()` returns a list after a successful, valid
response. Each item must have a nonblank string `name`, integer `points`
(not a boolean), and string `description`. Additional fields are preserved.
A malformed row fails the whole response; no partial card is used.

Failed HTTP reads, including 404, raise `MLAIBackendUnavailableError` with
reason code `rate_card_unavailable`. Invalid JSON or structure uses
`invalid_backend_response`. Existing typed timeout and circuit-breaker
errors pass through unchanged. The five-second timeout, one transport
retry, 0.25-second retry backoff, and circuit breaker are unchanged.

## Member-facing behavior

- A populated card keeps the standard rates heading and point descriptions.
- HTTP 200 with `[]` shows `No active point rates are configured.`
- A failed or invalid read shows
  `I couldn't load the rate card just now. Please try again in a moment.`

For an award with no amount, a valid matching rate still requires the
admin to confirm the suggestion. Failed reads stop before awarding points,
linking users, or notifying recipients. A valid empty card or no match
keeps the existing manual-amount question. Explicit-amount awards keep their
existing authorization and allowance checks and do not fetch the card.

## Local verification

With development-only values and dotenv disabled, from `roo-standalone`:

```bash
python -m pytest roo/tests/test_mlai_backend_client.py \
  roo/tests/test_rate_card.py \
  roo/tests/test_points_requests.py::test_execute_mlai_points_prefers_roo_api_key -q
```

These selected tests use mocked HTTP, provider, and Slack boundaries and
do not initialize a database or run migrations. The rest of the
points-request module includes migration helpers and is outside this
migration-free test selection. Local passes do not establish production
rates or deployment health. This repair requires no schema or rate-data
changes.
