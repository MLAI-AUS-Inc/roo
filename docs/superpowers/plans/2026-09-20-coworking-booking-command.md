# Coworking Booking Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give admins a deterministic, private `/coworking-today [YYYY-MM-DD]` command listing active coworking bookings independently of coworking reports.

**Architecture:** Add a read-only booking-list API in the Django backend and a focused command module in Roo. Route the exact slash command after signature, replay, and context checks; enforce admin access in the backend before querying bookings. Use a two-second overall lookup budget, returning a private retry message if the backend is slow, with no AI or background delivery.

**Tech Stack:** Existing Python, Django REST Framework, FastAPI, httpx, pytest, standard-library datetime/zoneinfo/asyncio.

**Spec:** [Approved conversational design](../specs/2026-09-20-coworking-booking-command-design.md). Read both files. The user approved the short design and requested this plan; implementation has not been requested yet.

## Global Constraints

- `/coworking-today` returns today's active bookings.
- `/coworking-today YYYY-MM-DD` returns active bookings for that date.
- Dates use `Australia/Melbourne`.
- Every response is private to the requester (`ephemeral`).
- Only active authorised admins can obtain the list.
- Show the selected date, the number of people booked, and their names.
- Exclude cancelled bookings; show an explicit empty result when nobody is booked.
- Use deterministic parsing, database lookup, and formatting, with no AI routing or generation.
- Keep this separate from existing coworking reports.
- No booking writes, arrival tracking, exports, schedules, or new dependencies are required.

## Review Focus

1. Signed non-admin requests and recently deactivated admins must never receive names; backend role checks precede lookup (Task 1).
2. UTC/Melbourne date boundaries and daylight-saving transitions must select the Melbourne calendar date (Task 2).
3. Invalid dates, extra arguments, and user-controlled markup must not invoke alternate commands or generate Slack mentions (Tasks 2–3).
4. Backend timeout, malformed responses, and service errors must give private errors rather than zero bookings or an AI fallback (Task 2).
5. Admin-surface allowlists, replay protection, and long booking lists must preserve privacy and avoid silently dropping names (Tasks 2–3).

## Repository map and execution preparation

Planning inspected these local snapshots, which are evidence of interfaces, not deployment state:

- Roo: `/Users/alanphilip/Documents/MLAI/roo-pr225-plan`, whose HEAD names `codex/meeting-room-booking`.
- Backend: `/Users/alanphilip/Documents/MLAI/backend-pr710-hardening` (`roo/views.py`); the booking model was also read in `mlai-backend/roo/models.py`.
- Roo `roo-standalone/roo/main.py:2956` owns `/slack/commands`; it verifies signatures, deduplicates requests, checks allowed contexts, then rejects all commands on the admin surface.
- Backend `roo/views.py:1508` owns `CoworkingViewSet`; its `report` action starts around line 1560. `get_permissions` already narrows `book_many` to `HasStrictRooApiKey`.
- Roo client `roo-standalone/roo/clients/mlai_backend.py` has `_request`, `_points_base = "/api/v1/points"`, and a separate `get_coworking_report` method. Do not use or change the report method.
- Roo `roo-standalone/roo/tests/test_slack_security.py` contains signed-request helpers, test settings, replay tests, and admin-context tests.

Start implementation from fresh branches based on the current intended base of each repository, using the worktrees skill if isolation is needed. Read applicable AGENTS.md files and compare these interfaces before editing. Some local file reads stalled during planning; current branch ancestry, URL composition, and CI test setup are not asserted verified. Do not implement on an unrelated existing feature branch merely because it was inspected.

An adjacent backend checkout's AGENTS.md requires explicit approval before running migration-backed Django tests. Follow the instructions in the selected checkout; this plan authorises no migration or deployment. No new migration is needed. Record any test blocked by that policy rather than claiming it passed.

All paths below are relative to the indicated repository root.

| Repository / file | Responsibility |
| --- | --- |
| Backend: create `roo/coworking_snapshot.py` | Active booking projection and stable ordering |
| Backend: modify `roo/views.py` | Authorised read action |
| Backend: modify `mlai/urls.py` | Explicit canonical URL, if not already supplied by the existing router |
| Backend: create `tests/test_coworking_snapshot.py` | Permission, filter, and wire-contract tests |
| Roo: modify `roo-standalone/roo/clients/mlai_backend.py` | Dedicated bounded API call |
| Roo: create `roo-standalone/roo/coworking_snapshot.py` | Date parsing, response validation, formatting and errors |
| Roo: modify `roo-standalone/roo/main.py` | Exact command dispatch only |
| Roo: create `roo-standalone/roo/tests/test_coworking_snapshot.py` | Client and command unit tests |
| Roo: modify `roo-standalone/roo/tests/test_slack_security.py` | Signed route integration tests |
| Roo: create `roo-standalone/docs/coworking-today-command.md` | Slack registration and operational smoke test |

## Task 1: Authorised booking-list API

**Interfaces:** Produces `GET /api/v1/points/coworking/bookings-for-date/?slack_user_id=U123&date=2026-09-21`, authenticated by the existing strict Roo service key. HTTP 200 returns `{"date":"2026-09-21","count":1,"people":[{"user_id":"42","name":"Alice Smith"}]}`. HTTP 400 is invalid/missing input; HTTP 403 is an unauthorised actor. Missing/invalid service credentials fail through the existing permission class. No point costs or ledger fields are returned.

**Files:** Backend files in the map above.

- [ ] **Step 1: Add database-backed projection tests.** Use the repository's existing isolated Django test settings and fixtures; do not use live data. The core test body is:

```python
from datetime import date
from django.test import TestCase
from core.models import User
from roo.models import CoworkingBooking
from roo.coworking_snapshot import build_booking_snapshot

class BookingSnapshotTests(TestCase):
    def test_only_selected_date_active_people(self):
        alice = User.objects.create(email="alice@example.test", first_name="Alice", last_name="Smith")
        ben = User.objects.create(email="ben@example.test", first_name="Ben", last_name="Jones")
        day = date(2026, 9, 21)
        CoworkingBooking.objects.create(user=alice, date=day, status="booked", points_cost=8)
        CoworkingBooking.objects.create(user=ben, date=day, status="cancelled", points_cost=8)
        CoworkingBooking.objects.create(user=ben, date=date(2026, 9, 22), status="booked", points_cost=8)
        self.assertEqual(build_booking_snapshot(day), {
            "date": "2026-09-21", "count": 1,
            "people": [{"user_id": str(alice.pk), "name": "Alice Smith"}],
        })
        self.assertEqual(build_booking_snapshot(date(2026, 9, 23)), {
            "date": "2026-09-23", "count": 0, "people": [],
        })
```

- [ ] **Step 2: Run the new test and observe the missing-module failure.** Once local test policy permits, run `python manage.py test tests.test_coworking_snapshot --verbosity 2` in Backend using its documented isolated settings. Never substitute production settings.

- [ ] **Step 3: Implement the projection.** Create `roo/coworking_snapshot.py`:

```python
from datetime import date
from .models import CoworkingBooking

def build_booking_snapshot(day: date) -> dict:
    users = {}
    for booking in CoworkingBooking.objects.filter(date=day, status="booked").select_related("user"):
        user = booking.user
        name = " ".join((user.full_name or "").split())
        users[str(user.pk)] = {"user_id": str(user.pk), "name": name or f"Member {user.pk}"}
    people = sorted(users.values(), key=lambda row: (row["name"].casefold(), row["user_id"]))
    return {"date": day.isoformat(), "count": len(people), "people": people}
```

- [ ] **Step 4: Add the authorised action tests before implementing the action.** In the same test module, add:

```python
from unittest.mock import patch
from django.test import SimpleTestCase
from rest_framework.test import APIRequestFactory
from roo.views import CoworkingViewSet

class BookingSnapshotAccessTests(SimpleTestCase):
    def test_denied_actor_never_reads_bookings(self):
        request = APIRequestFactory().get("/", {"slack_user_id": "UNONADMIN", "date": "2026-09-21"})
        with patch("roo.views.is_points_admin", return_value=False), patch("roo.views.build_booking_snapshot") as lookup:
            response = CoworkingViewSet.as_view({"get": "bookings_for_date"}, permission_classes=[])(request)
        self.assertEqual(response.status_code, 403)
        lookup.assert_not_called()
```

For this unit test only, bypass `get_permissions` with `patch.object(CoworkingViewSet, "get_permissions", return_value=[])` around the call; the existing override otherwise takes precedence over `permission_classes=[]`. Add a separate real permission test with `override_settings(ROO_API_KEY="synthetic-roo-key")`: no key must fail, `HTTP_X_API_KEY="synthetic-roo-key"` plus an allowed actor succeeds. Pin active/inactive membership using real `PointsAdmin` records and `is_points_admin`, rather than mocking role checks for that test. Verify the current helper's accepted full-admin roles before retaining it; do not substitute the broader report permission merely because this is called a report conversationally.

- [ ] **Step 5: Implement the action and strict permission mapping.** Import `re` and `build_booking_snapshot` in `roo/views.py`; add `bookings_for_date` to the existing strict-key branch of `get_permissions`. Insert into `CoworkingViewSet`:

```python
@action(detail=False, methods=["get"], url_path="bookings-for-date")
def bookings_for_date(self, request):
    actor = (request.query_params.get("slack_user_id") or "").strip()
    if not actor:
        return Response({"error": "slack_user_id is required"}, status=400)
    if not is_points_admin(actor):
        return Response({"error": "Only active Roo Points Admins can view coworking bookings"}, status=403)
    raw = request.query_params.get("date", "")
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            raise ValueError()
        day = date.fromisoformat(raw)
    except ValueError:
        return Response({"error": "Use YYYY-MM-DD"}, status=400)
    return Response(build_booking_snapshot(day))
```

Inspect the current URL resolver. If the router already exposes the canonical URL, add no duplicate. Otherwise add this explicit entry to `mlai/urls.py` using its existing `path` import and a `CoworkingViewSet` import:

```python
path("api/v1/points/coworking/bookings-for-date/",
     CoworkingViewSet.as_view({"get": "bookings_for_date"}),
     name="coworking-bookings-for-date"),
```

- [ ] **Step 6: Add contract cases and run the focused test module.** Test `2026-02-30`, `20260921`, missing date, and missing actor as 400; inactive/unknown actors as denied; stable alphabetical order and equal names with different IDs; blank names as `Member <id>`; cancelled-then-rebooked users once. Exercise the canonical URL with `APIClient.get`, not only `as_view`, to catch URL/auth mistakes. Assert successful keys are exactly `date`, `count`, `people` and each person's keys exactly `user_id`, `name`. Run the new tests plus existing coworking tests selected from current CI; existing report responses must remain unchanged.

- [ ] **Step 7: Commit the backend deliverable.** Stage only the files changed by Task 1 and commit `feat: add admin coworking booking snapshot endpoint`.

## Task 2: Deterministic command module and backend client

**Interfaces:** Consumes Task 1's JSON contract. Produces `MLAIBackendClient.get_coworking_snapshot(slack_user_id: str, booking_date: str) -> dict` and async `handle_command(text: str, user_id: str, client: MLAIBackendClient, *, now: datetime | None = None) -> dict`. The optional aware `now` is test injection only; returned dictionaries are Slack ephemeral payloads with `text` and, on success, plain-text blocks.

**Files:** Roo client, new command module and unit-test module from the map.

- [ ] **Step 1: Write failing date and validation tests.** Use `asyncio.run` so no new pytest plugin is needed:

```python
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock
import pytest
from roo.coworking_snapshot import handle_command

@pytest.mark.parametrize(("instant", "expected"), [
    ("2026-09-20T14:30:00+00:00", "2026-09-21"),
    ("2026-10-03T16:30:00+00:00", "2026-10-04"),
    ("2026-04-04T16:30:00+00:00", "2026-04-05"),
])
def test_melbourne_today(instant, expected):
    client = AsyncMock()
    client.get_coworking_snapshot.return_value = {"date": expected, "count": 0, "people": []}
    result = asyncio.run(handle_command("", "UADMIN", client, now=datetime.fromisoformat(instant)))
    client.get_coworking_snapshot.assert_awaited_once_with("UADMIN", expected)
    assert result["response_type"] == "ephemeral"
    assert "No active bookings" in result["text"]

@pytest.mark.parametrize("text", ["tomorrow", "20260921", "2026-02-30", "2026-09-21 extra", "connect github"])
def test_bad_arguments_never_call_backend(text):
    client = AsyncMock()
    result = asyncio.run(handle_command(text, "UADMIN", client))
    assert "Usage:" in result["text"]
    client.get_coworking_snapshot.assert_not_awaited()
```

- [ ] **Step 2: Run `python -m pytest roo/tests/test_coworking_snapshot.py -q` from `roo-standalone`; expect import failure.**

- [ ] **Step 3: Add the dedicated client method.** Use the existing client error normalisation:

```python
async def get_coworking_snapshot(self, slack_user_id: str, booking_date: str) -> dict:
    response = await self._request(
        "GET", f"{self._points_base}/coworking/bookings-for-date/",
        params={"slack_user_id": self._clean_slack_id(slack_user_id), "date": booking_date},
        timeout=1.5, transport_retries=0, circuit_breaker=True,
    )
    self._raise_for_status_or_backend_unavailable(response)
    return response.json()
```

- [ ] **Step 4: Implement the command module.** Keep the API lookup inside a two-second total budget; this includes connection establishment and reading. Use plain-text blocks to prevent stored names from becoming mentions. If an unusually long list cannot fit, explicitly report that it is too large rather than truncate people silently.

```python
from __future__ import annotations
import asyncio
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
import httpx

MELBOURNE = ZoneInfo("Australia/Melbourne")
LOOKUP_BUDGET = 2.0
USAGE = "Usage: /coworking-today [YYYY-MM-DD]"

def private(text: str) -> dict:
    return {"response_type": "ephemeral", "text": text}

def render_snapshot(data: dict, day: date) -> dict:
    if not isinstance(data, dict) or data.get("date") != day.isoformat():
        raise ValueError("Unexpected snapshot date")
    people = data.get("people")
    count = data.get("count")
    if not isinstance(people, list) or type(count) is not int or count != len(people):
        raise ValueError("Invalid snapshot count")
    seen = set()
    names = []
    for person in people:
        if not isinstance(person, dict):
            raise ValueError("Invalid person")
        ident, name = person.get("user_id"), person.get("name")
        if not isinstance(ident, str) or not ident or ident in seen or not isinstance(name, str) or not name.strip():
            raise ValueError("Invalid person")
        seen.add(ident)
        names.append(" ".join(name.split()))
    heading = f"Coworking bookings · {day.day} {day.strftime('%B %Y')}"
    summary = f"{count} {'person' if count == 1 else 'people'} booked"
    lines = [heading, summary] + ([f"• {name}" for name in names] if names else ["No active bookings for this date."])
    chunks = []
    for line in lines:
        if len(line) > 2900:
            return private("The booking list is too large to display safely in Slack. Please contact the Roo maintainer.")
        if chunks and len(chunks[-1]) + len(line) + 1 <= 2900:
            chunks[-1] += "\n" + line
        else:
            chunks.append(line)
    if len(chunks) > 45:
        return private("The booking list is too large to display safely in Slack. Please contact the Roo maintainer.")
    return {
        "response_type": "ephemeral",
        "text": heading + " — " + summary + (" — No active bookings for this date." if not count else ""),
        "blocks": [{"type": "section", "text": {"type": "plain_text", "text": chunk}} for chunk in chunks],
    }

async def handle_command(text: str, user_id: str, client, *, now: datetime | None = None) -> dict:
    raw = text.strip()
    try:
        if raw:
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
                raise ValueError()
            day = date.fromisoformat(raw)
        else:
            day = (now or datetime.now(MELBOURNE)).astimezone(MELBOURNE).date()
    except ValueError:
        return private(USAGE)
    if not user_id:
        return private("Unable to identify the requester. Please run the command again.")
    try:
        data = await asyncio.wait_for(client.get_coworking_snapshot(user_id, day.isoformat()), LOOKUP_BUDGET)
        return render_snapshot(data, day)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            return private("Only active Roo Points Admins can view coworking bookings.")
        return private("Couldn't load coworking bookings. Please try again.")
    except Exception:
        return private("Couldn't load coworking bookings. Please try again.")
```

Keep diagnostics free of names, keys, and response bodies; record a concise exception class through the existing logger if adding logging. Do not catch cancellation separately or change generic client behaviour.

- [ ] **Step 5: Add boundary and failure tests.** The concrete core is:

```python
from roo import coworking_snapshot as command
from roo.coworking_snapshot import render_snapshot
from datetime import date

@pytest.mark.parametrize("data", [None, {}, {"date":"2026-09-21","count":1,"people":[]}])
def test_malformed_backend_is_not_empty_success(data):
    client = AsyncMock()
    client.get_coworking_snapshot.return_value = data
    result = asyncio.run(handle_command("2026-09-21", "UADMIN", client))
    assert result == command.private("Couldn't load coworking bookings. Please try again.")

def test_timeout_is_private(monkeypatch):
    monkeypatch.setattr(command, "LOOKUP_BUDGET", 0.01)
    async def slow(*args):
        await asyncio.sleep(10)
    client = AsyncMock()
    client.get_coworking_snapshot.side_effect = slow
    result = asyncio.run(handle_command("2026-09-21", "UADMIN", client))
    assert "try again" in result["text"]
    assert result["response_type"] == "ephemeral"

def test_names_are_literal_and_list_is_chunked():
    people = [{"user_id": str(i), "name": f"Member {i} <!channel> & <@U123>"} for i in range(150)]
    result = render_snapshot({"date":"2026-09-21", "count":150, "people":people}, date(2026,9,21))
    assert len(result["blocks"]) > 1
    assert all(b["text"]["type"] == "plain_text" and len(b["text"]["text"]) <= 2900 for b in result["blocks"])
    body = "\n".join(b["text"]["text"] for b in result["blocks"])
    assert all(p["name"] in body for p in people)
```

Also pin the client wire contract with `_request = AsyncMock(return_value=httpx.Response(200, request=httpx.Request("GET", "https://backend.example.test/"), json={"date":"2026-09-21","count":0,"people":[]}))`; assert exact GET path, actor/date params, timeout, and no retries. Assert HTTP 403 maps to denial, 500/network errors map to retry, explicit dates ignore `now`, and names with line breaks remain one person. Missing actor must make no backend call.

- [ ] **Step 6: Run the focused tests and commit.** Run `python -m pytest roo/tests/test_coworking_snapshot.py -q`; commit only these Roo changes as `feat: implement deterministic coworking snapshot command`.

## Task 3: Signed Slack routing, registration instructions, and acceptance checks

**Interfaces:** Consumes `handle_command` and the client from Task 2. Uses the existing `POST /slack/commands`; the exact command name is `/coworking-today`. Existing Slack signatures, receipt deduplication and allowed-context checks remain authoritative.

**Files:** Roo `main.py`, `test_slack_security.py`, and the new command documentation.

- [ ] **Step 1: Add a signed command integration test using the existing test helpers.** Append to `test_slack_security.py`:

```python
def test_coworking_command_uses_deterministic_handler(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    configured = _settings(tmp_path)
    main_module.app.dependency_overrides[get_settings] = lambda: configured
    handler = AsyncMock(return_value={"response_type":"ephemeral", "text":"2 people booked"})
    monkeypatch.setattr(main_module, "handle_coworking_command", handler)
    def no_agent(*args, **kwargs):
        raise AssertionError("AI path must not run")
    monkeypatch.setattr(main_module, "get_agent", no_agent)
    body = urlencode({"command":"/coworking-today", "text":"2026-09-21", "user_id":"UADMIN", "channel_id":"C123"}).encode()
    headers = _signed_headers(configured.SLACK_SIGNING_SECRET, int(time.time()), body, "application/x-www-form-urlencoded")
    client = TestClient(main_module.app)
    result = client.post("/slack/commands", content=body, headers=headers)
    assert result.json() == {"response_type":"ephemeral", "text":"2 people booked"}
    assert handler.await_count == 1
    assert handler.call_args.args[:2] == ("2026-09-21", "UADMIN")
    assert client.post("/slack/commands", content=body, headers=headers).json() == {}
    assert handler.await_count == 1
```

- [ ] **Step 2: Run the single test; expect the missing handler attribute failure.** Run `python -m pytest roo/tests/test_slack_security.py::test_coworking_command_uses_deterministic_handler -q` from `roo-standalone`.

- [ ] **Step 3: Add the narrow dispatcher.** Import `handle_command as handle_coworking_command` from `.coworking_snapshot`. Immediately after the existing `_is_slack_context_allowed` rejection and before `if settings.ROO_SURFACE == "admin"`, insert:

```python
if command == "/coworking-today":
    from .clients.mlai_backend import MLAIBackendClient
    client = MLAIBackendClient(
        base_url=settings.MLAI_BACKEND_URL,
        api_key=settings.ROO_API_KEY,
        surface=settings.ROO_SURFACE,
    )
    return await handle_coworking_command(text, user_id, client)
```

Use the actual strict Roo key required by Task 1; do not configure a generic key to broaden access. Missing key/service configuration must yield the command's private failure response. Keep the existing admin rejection for all other commands. Actor identity comes exclusively from the verified Slack form, never from command text.

- [ ] **Step 4: Extend the signed test matrix.** Parameterise the test settings with public surface, allowed admin channel, and allowed admin DM using existing context tests as fixtures. For an outside-allowlist channel assert the existing context denial and `handler.assert_not_awaited()`. Tamper with the signed body and assert 403 and no handler call. Send `/roo` on admin surface and assert the old pilot denial. Send `/coworking-today` with text `connect github` and assert the handler receives it, yielding usage, with no GitHub handler or AI call. Keep each request signature unique between independent cases; reuse an identical signature only for the replay case.

- [ ] **Step 5: Write the registration runbook with these exact settings.** The documentation must include:

```text
Command: /coworking-today
Request URL: the selected Roo deployment's existing HTTPS /slack/commands URL
Short description: Privately list coworking bookings for a date
Usage hint: [YYYY-MM-DD]
Escape channels, users, and links: off
Required OAuth scope: commands
Backend setting: ROO_API_KEY shared with the selected Roo deployment
Examples: /coworking-today
          /coworking-today 2026-09-21
```

Use the existing installed app that owns the intended deployment; do not register the same command on multiple workspace apps. Record the actual app/deployment URL during rollout rather than inventing it in this plan. If the app uses a tracked manifest, add the command there; otherwise document the Slack app dashboard change. Refresh installation if Slack requires it. No new bot-message scopes or response-URL callbacks are needed. The user must authorise rollout separately; this planning task does not change Slack configuration.

The runbook must explain that results show active bookings at request time, not physical presence, and that slow requests return a retry message. A backend-first rollout avoids exposing a command before its endpoint exists. Register the command only after both services pass staging checks. Rollback: remove the Slack command registration; additive backend code can remain inert.

- [ ] **Step 6: Run regression and staging acceptance checks.** Locally run `python -m pytest roo/tests/test_coworking_snapshot.py roo/tests/test_slack_security.py -q`, then the repository's existing targeted coworking/client tests. Run the backend tests from Task 1 under approved isolated settings. In an authorised staging workspace, an admin must see only the active selected-date members; a non-admin must see only denial; invalid arguments must show usage; no-booking dates must explicitly say none; responses must remain private even when invoked in a public channel. Verify the HTTP handler responds within Slack's three-second limit under a deliberately delayed backend and shows the private retry response. Confirm the ordinary coworking report still works and no AI invocation occurs for any tested command path.

Slack requires acknowledgement within three seconds and supports ephemeral slash-command replies: [official Slack documentation](https://docs.slack.dev/interactivity/implementing-slash-commands/). The two-second application lookup budget reserves time for request verification and response overhead; staging measurement is required before enabling the command.

- [ ] **Step 7: Commit the integrated deliverable.** Stage only Task 3's files; commit `feat: expose private coworking bookings slash command`.

## Completion and handoff

Implementation is complete when both repository test suites selected above pass, the new endpoint and exact command are wired, and the runbook is ready. Report staging/registration as outstanding until actually performed; do not claim the command is live based on local tests. No implementation, deployment, Slack configuration change, or migration was performed while writing this plan.

Recommended execution: **Native**. The three tasks form one small sequential backend-to-command flow, so one implementer can keep the contract consistent; a final independent review should focus on role enforcement and Slack routing. Subagent-driven execution is available if per-task independent review is preferred.
