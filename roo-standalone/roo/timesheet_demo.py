"""Local Roo Dev timesheet demo: synthetic Linear data, real test-thread replies.

Run explicitly with --env-file. This entry point never imports the agent, calls
Linear, runs scheduled payments, or changes the production recipient policy.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import replace
import json
from pathlib import Path
import re
from types import SimpleNamespace

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException, Request
import httpx
import uvicorn

from .slack_security import SlackRequestVerificationError, verify_slack_request
from .timesheet_commands import enqueue_event
from .timesheet_worker import ReportAPI, TimesheetService
from .timesheets import TimesheetConfig, TimesheetError, PROJECTS, artifacts, summary, timestamp

DEV_APP = 'A0BT0N95022'
DEV_BOT = 'U0BT0NC5M46'
DEV_TEAM = 'T05N9C1QSJC'
DEV_CHANNEL = 'C0BRM181EDV'
FIXTURE = 'studio-timesheet-demo-v1'
ALICE = '00000000-0000-0000-0000-000000000001'
BOB = '00000000-0000-0000-0000-000000000002'
NOTICE = ('*Roo Dev test — FAKE DATA.* This report is replying in your thread for testing. '
          '*In production, Roo will DM the report directly to Dr Sam, rather than reply in this thread.*')


def fixture():
    """Two periods: first 8.25h + one exception; second 5h after the size fix."""
    def ticket(number, owner, size, **changes):
        issue = {'id': f'demo-{number}', 'identifier': f'DEMO-{number}', 'title': f'Synthetic task {number}',
                 'url': f'https://example.invalid/demo/DEMO-{number}',
                 'createdAt': '2026-08-20T00:00:00Z', 'updatedAt': '2026-09-24T00:00:00Z',
                 'completedAt': '2026-09-10T00:00:00Z', 'trashed': False,
                 'state': {'id': 'done', 'type': 'completed'}, 'assignee': {'id': owner},
                 'project': {'id': next(iter(PROJECTS))},
                 'labels': [{'id': 'size', 'name': size}] if size else [], **changes}
        return {'issue': issue, 'history': []}
    data = [ticket(1, ALICE, 'Extra Small (XS)'), ticket(2, ALICE, 'Small (S)'),
            ticket(3, BOB, 'Extra Large (XL)'), ticket(4, ALICE, 'Extra Large (XL)'),
            ticket(5, BOB, 'Large (L)', completedAt='2026-09-11T02:01:00Z'),
            ticket(6, BOB, 'Medium (M)'),
            ticket(7, ALICE, 'Extra Large (XL)', completedAt=None, state={'id': 'todo', 'type': 'unstarted'})]
    data[3]['history'] = [{'id': 'size-edit', 'createdAt': '2026-09-12T00:00:00Z',
        'addedLabelIds': ['size'], 'removedLabelIds': ['medium'],
        'removedLabels': [{'id': 'medium', 'name': 'Medium (M)'}]}]
    data[4]['history'] = [{'id': 'late-done', 'createdAt': '2026-09-11T02:01:00Z',
        'fromStateId': 'todo', 'toStateId': 'done', 'fromState': {'id': 'todo', 'type': 'unstarted'},
        'toState': {'id': 'done', 'type': 'completed'}}]
    data[5]['history'] = [{'id': 'fixed-size', 'createdAt': '2026-09-20T00:00:00Z', 'addedLabelIds': ['size']}]
    return data


class DemoAPI(ReportAPI):
    def __init__(self, client, token):
        super().__init__(client, linear_key='synthetic-no-linear-access', slack_token=token)

    def linear(self, *args, **kwargs):
        raise TimesheetError('linear_disabled_in_demo')

    def verify(self, config):
        auth = self.slack('auth.test')
        if auth.get('team_id') != DEV_TEAM or auth.get('user_id') != DEV_BOT:
            raise TimesheetError('roo_dev_identity_required')
        user = self.slack('users.info', {'user': DEV_BOT}).get('user') or {}
        if (user.get('profile') or {}).get('api_app_id') != DEV_APP:
            raise TimesheetError('roo_dev_app_required')

    def collect(self, config, end, deferred):
        return deepcopy(fixture())


class DemoService(TimesheetService):
    def __init__(self, config, api):
        if type(api) is not DemoAPI or config.scheduled or config.team != DEV_TEAM:
            raise TimesheetError('isolated_demo_required')
        super().__init__(config, api)

    def report(self, *args, **kwargs):
        report = super().report(*args, **kwargs)
        report['fixture'] = FIXTURE
        return report

    def request_authorized(self, request):
        return (request.get('team') == DEV_TEAM and request.get('channel') == DEV_CHANNEL
                and request.get('actor') in self.config.allowed_users)

    def deliver_request(self, report, request, key, now):
        channel, thread = request.get('channel'), request.get('thread_ts')
        if (report.get('fixture') != FIXTURE or request.get('actor') not in self.config.allowed_users
                or request.get('team') != DEV_TEAM or channel != DEV_CHANNEL
                or not re.fullmatch(r'\d+\.\d+', str(thread or ''))):
            raise TimesheetError('invalid_demo_destination')
        self.api.verify(self.config)
        self.api.verify_recipient(request['actor'], DEV_TEAM)
        text = NOTICE + '\n\n' + summary(report)
        text += '\n\n*Included synthetic tickets*\n' + ('\n'.join(
            f"• {r['identifier']} — {r['builder']}: {r['units']/4:g}h ({r['size']})" for r in report['rows']) or 'None')
        text += '\n\nFixture clock: ' + request['requested_at'] + '. Example links are placeholders.'
        parts = [{'kind': 'message', 'content': text, 'status': 'pending'}]
        parts += [{'kind': 'file', 'filename': 'FAKE-DATA-' + name, 'content': value, 'status': 'pending'}
                  for name, value in artifacts(report).items()]
        def post(destination, content):
            response = self.api.slack('chat.postMessage', {'channel': destination, 'thread_ts': thread,
                'text': content, 'unfurl_links': False, 'unfurl_media': False, 'reply_broadcast': False})
            if response.get('channel') != channel or not response.get('ts'):
                raise TimesheetError('unconfirmed_demo_delivery')
            return response['ts']
        def upload(destination, filename, content):
            return self.api._upload_csv(destination, filename, content, thread_ts=thread)
        # Bind receipt identity to the signed request's actor, channel and root.
        return self._deliver_transport(parts, request['actor'] + ':' + channel + ':' + thread,
            key, now, lambda _: channel, post, upload)


def demo_config(env):
    if env.get('ROO_ENVIRONMENT') != 'development' or env.get('TIMESHEET_DEMO_ENABLED') != 'true':
        raise TimesheetError('explicit_development_demo_required')
    allowed = env.get('TIMESHEET_DEMO_ALLOWED_SLACK_IDS', '')
    if not allowed or not env.get('SLACK_SIGNING_SECRET') or not env.get('SLACK_BOT_TOKEN'):
        raise TimesheetError('demo_configuration_missing')
    root = Path(env.get('TIMESHEET_DEMO_DATA_DIR', './data/timesheet-demo')).resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = root / 'synthetic-data-only'
    if not marker.exists() and any(root.iterdir()):
        raise TimesheetError('empty_demo_directory_required')
    marker.touch(mode=0o600)
    config = TimesheetConfig.from_env({'TIMESHEETS_ENABLED': 'true',
        'TIMESHEET_SLACK_TEAM_ID': DEV_TEAM, 'TIMESHEET_LINEAR_ORGANIZATION_ID': '00000000-0000-0000-0000-000000000003',
        'TIMESHEET_RECIPIENT_SLACK_ID': allowed.split(',')[0].strip(),
        'TIMESHEET_DATA_DIR': str(root / 'reports'), 'TIMESHEET_QUEUE_DIR': str(root / 'queue'),
        'TIMESHEET_BUILDERS_JSON': json.dumps([
            {'linear_user_id': ALICE, 'slack_user_id': 'UDEMOALICE', 'name': 'Alice (fake)'},
            {'linear_user_id': BOB, 'slack_user_id': 'UDEMOBOB', 'name': 'Bob (fake)'}])})
    testers = tuple(x.strip() for x in allowed.split(',') if x.strip())
    if any(not re.fullmatch(r'[UW][A-Z0-9]+', user) for user in testers):
        raise TimesheetError('invalid_demo_testers')
    return replace(config, allowed_users=testers)


def create_app(env, api):
    config = demo_config(env)
    service = DemoService(config, api)
    as_of = timestamp(env.get('TIMESHEET_DEMO_AS_OF', '2026-09-13T12:00:00+10:00'))
    settings = SimpleNamespace(ROO_SURFACE='public', TIMESHEET_COMMANDS_ENABLED=True,
        TIMESHEET_SLACK_TEAM_ID=DEV_TEAM, TIMESHEET_SLACK_BOT_USER_ID=DEV_BOT,
        TIMESHEET_FIRST_CUTOFF='2026-09-11',
        TIMESHEET_QUEUE_DIR=str(config.queue))

    @asynccontextmanager
    async def lifespan(app):
        await asyncio.to_thread(api.verify, config)
        for user in config.allowed_users:
            await asyncio.to_thread(api.verify_recipient, user, DEV_TEAM)
        stop = asyncio.Event()
        async def worker():
            while not stop.is_set():
                try:
                    result = await asyncio.to_thread(service.commands, datetime.now(timezone.utc))
                    if result:
                        print(json.dumps({'demo_requests': result}), flush=True)
                except Exception as exc:
                    print(json.dumps({'demo_error': type(exc).__name__}), flush=True)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
        task = asyncio.create_task(worker())
        app.state.ready = True
        try:
            yield
        finally:
            app.state.ready = False
            stop.set()
            await task

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service

    @app.get('/healthz/ready')
    async def ready():
        if not getattr(app.state, 'ready', False):
            raise HTTPException(503)
        return {'status': 'ready', 'mode': 'fake-timesheet-demo', 'as_of': as_of.isoformat()}

    @app.post('/slack/events')
    async def events(request: Request):
        body = await request.body()
        try:
            verify_slack_request(signing_secret=env['SLACK_SIGNING_SECRET'], raw_body=body,
                timestamp=request.headers.get('X-Slack-Request-Timestamp', ''),
                signature=request.headers.get('X-Slack-Signature', ''))
        except SlackRequestVerificationError:
            raise HTTPException(403, 'Invalid Slack signature')
        try:
            payload = json.loads(body)
        except ValueError:
            raise HTTPException(400, 'Invalid JSON')
        if payload.get('type') == 'url_verification':
            return {'challenge': payload.get('challenge')}
        event = payload.get('event') or {}
        if (payload.get('api_app_id') != DEV_APP or payload.get('team_id') != DEV_TEAM
                or event.get('user') not in config.allowed_users or event.get('channel') != DEV_CHANNEL):
            return {}
        try:
            # The separate fake-data app has already verified its pinned app,
            # workspace, test channel and tester. Never mutate shared settings.
            requester_settings = SimpleNamespace(**vars(settings), TIMESHEET_RECIPIENT_SLACK_ID=event['user'])
            await asyncio.to_thread(enqueue_event, requester_settings, payload, as_of)
        except Exception:
            raise HTTPException(503, 'Could not persist demo request')
        return {}

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--port', type=int, default=8012)
    args = parser.parse_args()
    # Deliberately ignore inherited production/environment credentials.
    env = dotenv_values(args.env_file)
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        api = DemoAPI(client, env.get('SLACK_BOT_TOKEN'))
        uvicorn.run(create_app(env, api), host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
