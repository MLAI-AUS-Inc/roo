import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys
import time

import pytest

from roo.hermes import HermesRunRequest, HermesRunner, permission_fingerprint
from roo.hermes.artifacts import ArtifactError, collect_artifacts


FAKE_ACP = r'''
import json, os, pathlib, signal, sys, time
mode = sys.argv[1]
workspace = pathlib.Path.cwd()
def send(message):
    print(json.dumps({'jsonrpc': '2.0', **message}), flush=True)
def read():
    line = sys.stdin.readline()
    return json.loads(line) if line else None
def respond(request, result):
    send({'id': request['id'], 'result': result})
initial = read()
assert initial['method'] == 'initialize' and initial['params']['protocolVersion'] == 1
pathlib.Path('environment.json').write_text(json.dumps(dict(os.environ)))
if mode == 'stderr_flood':
    sys.stderr.write('X' * 100000); sys.stderr.flush(); time.sleep(30)
if mode == 'hang':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    pathlib.Path('pid').write_text(str(os.getpid()))
    time.sleep(30)
if mode == 'bad_json':
    print('not-json', flush=True); time.sleep(30)
if mode == 'bad_response':
    send({'id': 99, 'result': {}}); time.sleep(30)
respond(initial, {'protocolVersion': 1, 'agentCapabilities': {'mcpCapabilities': {'http': mode != 'no_http'}}})
new = read()
if not new: sys.exit(0)
assert new['method'] == 'session/new'
assert new['params']['cwd'] == str(workspace)
assert len(new['params']['mcpServers']) == 1
assert new['params']['mcpServers'][0]['type'] == 'http'
if mode == 'early_update':
    send({'method': 'session/update', 'params': {'sessionId': 's1', 'update': {'sessionUpdate': 'available_commands_update', 'availableCommands': []}}})
respond(new, {'sessionId': 's1'})
prompt = read()
assert prompt['method'] == 'session/prompt'
assert not prompt['params']['prompt'][0]['text'].startswith('/')
if mode in ('deny', 'allow', 'permission_replay', 'approval_reuse', 'wrong_permission_session', 'permission_secret', 'permission_long'):
    tool = {'toolCallId': 'perm-check-1', 'kind': 'execute', 'rawInput': {'command': 'rm demo.txt', 'description': 'delete demo'}}
    if mode == 'permission_secret': tool['rawInput']['command'] = new['params']['mcpServers'][0]['headers'][0]['value']
    if mode == 'permission_long': tool['rawInput']['command'] = 'X' * 9000
    replies = []
    for index in range(2 if mode in ('permission_replay', 'approval_reuse') else 1):
        send({'id': 101 if mode == 'permission_replay' else 101 + index, 'method': 'session/request_permission', 'params': {'sessionId': 'other' if mode == 'wrong_permission_session' else 's1', 'toolCall': tool, 'options': [{'optionId': 'allow', 'kind': 'allow_once', 'name': 'Allow once'}, {'optionId': 'always', 'kind': 'allow_always', 'name': 'Always'}]}})
        reply = read()
        if reply is None: sys.exit(0)
        replies.append(reply)
        pathlib.Path('permissions.json').write_text(json.dumps(replies))
if mode == 'unsupported_callback':
    send({'id': 100, 'method': 'terminal/create', 'params': {'command': 'touch escaped'}})
    reply = read()
    assert reply['error']['code'] == -32601
if mode == 'artifact':
    pathlib.Path('artifacts').mkdir()
    pathlib.Path('artifacts/report.md').write_text('A safe report')
if mode == 'artifact_secret':
    pathlib.Path('artifacts').mkdir()
    pathlib.Path('artifacts/leak').write_text(new['params']['mcpServers'][0]['headers'][0]['value'])
text = 'Finished'
if mode == 'secret_text': text = new['params']['mcpServers'][0]['headers'][0]['value']
send({'method': 'session/update', 'params': {'sessionId': 'other' if mode == 'wrong_session' else 's1', 'update': {'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': text}}}})
respond(prompt, {'stopReason': 'refusal' if mode == 'refusal' else 'end_turn'})
while read(): pass
'''


@pytest.fixture
def setup_runner(tmp_path):
    root = tmp_path.resolve()
    script = root / "fake_acp.py"
    script.write_text(FAKE_ACP)
    request = HermesRunRequest(
        task_id="task-1", prompt="Create a report", workspace=root / "work", state_dir=root / "state",
        model="allowed-model", broker_url="http://broker/mcp", broker_token="task-secret-123",
        model_proxy_url="http://broker/v1", model_proxy_token="model-secret-456",
    )

    def make(mode="success", **kwargs):
        return HermesRunner([sys.executable, str(script), mode], trusted_broker_url=request.broker_url,
                            trusted_model_proxy_url=request.model_proxy_url,
                            termination_grace_seconds=0.1, **kwargs)
    return request, make


@pytest.mark.asyncio
async def test_session_protocol_artifact_and_no_parent_credentials(setup_runner, monkeypatch):
    request, make = setup_runner
    for key in ("ORG_BRAIN_API_KEY", "SLACK_BOT_TOKEN", "OPENAI_API_KEY", "SSH_AUTH_SOCK", "PYTHONPATH", "DOCKER_HOST"):
        monkeypatch.setenv(key, "must-not-reach-agent")
    result = await make("artifact").run(request)
    assert result.status == "completed"
    assert result.text == "Finished"
    assert result.artifacts[0].relative_path == "report.md"
    assert result.artifacts[0].content == b"A safe report"
    env = json.loads((request.workspace / "environment.json").read_text())
    assert "must-not-reach-agent" not in env.values()
    config = json.loads((request.state_dir / "config.yaml").read_text())
    assert config["model"]["base_url"] == request.model_proxy_url
    assert config["model"]["api_key"] == request.model_proxy_token
    assert "browser" not in config["platform_toolsets"]["acp"]
    assert (request.state_dir / "config.yaml").stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="fresh"):
        await make().run(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,error", [("no_http", "unsupported_capabilities"), ("bad_json", "invalid_json"),
                                         ("bad_response", "unexpected_response"), ("wrong_session", "wrong_session"),
                                         ("permission_replay", "replayed_request"), ("refusal", "agent_refusal")])
async def test_protocol_failures_are_safe(setup_runner, mode, error):
    request, make = setup_runner
    result = await make(mode).run(request)
    assert result.status == "failed"
    assert result.error_code == error
    assert request.broker_token not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["early_update", "unsupported_callback"])
async def test_supported_early_notifications_and_denied_client_execution(setup_runner, mode):
    request, make = setup_runner
    assert (await make(mode).run(request)).status == "completed"
    assert not (request.workspace / "escaped").exists()


def approval(request):
    return permission_fingerprint(request.task_id, {"kind": "execute", "rawInput": {
        "command": "rm demo.txt", "description": "delete demo"}})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,approved,outcomes,status", [
    ("deny", False, ["cancelled"], "approval_required"),
    ("allow", True, ["selected"], "completed"),
    ("approval_reuse", True, ["selected", "cancelled"], "approval_required"),
    ("wrong_permission_session", True, ["cancelled"], "approval_required"),
])
async def test_exact_approval_is_one_use_and_session_bound(setup_runner, mode, approved, outcomes, status):
    request, make = setup_runner
    if approved:
        request = replace(request, approved_permissions=frozenset({approval(request)}))
    result = await make(mode).run(request)
    assert result.status == status
    replies = json.loads((request.workspace / "permissions.json").read_text())
    assert [r["result"]["outcome"]["outcome"] for r in replies] == outcomes
    assert all(r["result"]["outcome"].get("optionId") != "always" for r in replies)
    assert permission_fingerprint("other-task", {"kind": "execute", "rawInput": {"command": "rm demo.txt", "description": "delete demo"}}) != approval(request)


@pytest.mark.asyncio
async def test_deadline_escalates_when_worker_ignores_term(setup_runner):
    request, make = setup_runner
    before = time.monotonic()
    result = await make("hang").run(replace(request, timeout_seconds=0.25))
    assert result.status == "timed_out" and time.monotonic() - before < 3
    pid = int((request.workspace / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_cancellation_cleans_process_and_propagates(setup_runner):
    request, make = setup_runner
    job = asyncio.create_task(make("hang").run(request))
    for _ in range(100):
        if (request.workspace / "pid").exists():
            break
        await asyncio.sleep(0.01)
    pid = int((request.workspace / "pid").read_text())
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.asyncio
async def test_stderr_is_bounded_without_returning_diagnostics(setup_runner):
    request, make = setup_runner
    result = await make("stderr_flood").run(replace(request, max_output_bytes=4096))
    assert result.error_code == "diagnostic_limit"
    assert "XXX" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status", [("secret_text", "completed"), ("artifact_secret", "failed")])
async def test_task_tokens_do_not_escape_in_outputs(setup_runner, mode, status):
    request, make = setup_runner
    result = await make(mode).run(request)
    assert result.status == status and request.broker_token not in result.text
    assert not result.artifacts


@pytest.mark.asyncio
async def test_untrusted_endpoint_rejected_before_process(setup_runner):
    request, make = setup_runner
    with pytest.raises(ValueError):
        await make().run(replace(request, broker_url="http://metadata.internal/mcp"))
    assert not request.workspace.exists()


@pytest.mark.asyncio
async def test_member_prompt_cannot_become_editor_control_command(setup_runner):
    request, make = setup_runner
    assert (await make().run(replace(request, prompt="/yolo"))).status == "completed"


@pytest.mark.parametrize("kind", ["symlink", "directory_symlink", "hardlink", "fifo", "oversize", "count"])
def test_artifact_escape_and_resource_bounds(tmp_path, kind):
    work = tmp_path / "work"
    export = work / "artifacts"
    export.mkdir(parents=True)
    secret = tmp_path / "secret"
    secret.write_text("hidden")
    if kind == "symlink":
        (export / "a").symlink_to(secret)
    elif kind == "directory_symlink":
        (export / "a").symlink_to(tmp_path, target_is_directory=True)
    elif kind == "hardlink":
        os.link(secret, export / "a")
    elif kind == "fifo":
        os.mkfifo(export / "a")
    elif kind == "oversize":
        (export / "a").write_bytes(b"x" * 20)
    else:
        (export / "a").write_bytes(b"a")
        (export / "b").write_bytes(b"b")
    with pytest.raises(ArtifactError):
        collect_artifacts(work, max_bytes=10, max_files=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,reviewable,truncated", [("deny", True, False), ("permission_secret", False, False), ("permission_long", False, True)])
async def test_permission_review_details_are_bounded_and_secret_redacted(setup_runner, mode, reviewable, truncated):
    request, make = setup_runner
    result = await make(mode).run(request)
    assert result.status == "approval_required"
    assert len(result.permission_details) == 1
    detail = result.permission_details[0]
    assert detail["fingerprint"] == result.permission_requests[0]
    assert detail["reviewable"] is reviewable and detail["truncated"] is truncated
    assert len(detail["command"]) <= 8192
    assert request.broker_token not in repr(result)


def test_opt_in_browser_selects_only_the_reviewed_text_toolset(setup_runner):
    from roo.hermes.runner import build_config
    request, _ = setup_runner
    config = build_config(replace(request, browser_enabled=True), "/workspace")
    assert config["platform_toolsets"]["acp"] == ["terminal", "file", "todo", "roo_browser_text"]
    assert config["browser"]["backend"] == "off"
    assert config["browser"]["use_real_profile"] is False
