import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from roo.hermes import HermesRunResult
from roo.proactive.client import BackendClient, BackendError, ClaimedTask, CompletionUnknown
from roo.proactive.config import WorkerConfig, root_url
from roo.proactive.docker import DockerTaskLauncher
from roo.proactive.worker import Worker, reconcile_pending


def config(tmp_path):
    return WorkerConfig(
        enabled=True, backend_origin="https://backend.invalid", worker_key="controller-" + "s" * 40,
        broker_origin="http://broker:8090", model_origin="http://broker:8090",
        image="roo-worker@sha256:" + "a" * 64, state_root=tmp_path,
        runtime_uid=os.getuid(), runtime_gid=os.getgid(),
    )


def task():
    return ClaimedTask(str(uuid4()), str(uuid4()), 1, "Create a report", "test-model",
                       "task-tool-" + "a" * 40, "task-model-" + "b" * 40)


def test_disabled_does_not_require_or_load_any_secrets():
    assert WorkerConfig.from_env({}).enabled is False
    assert WorkerConfig.from_env({"ORG_BRAIN_API_KEY": "never-use"}).worker_key == ""


@pytest.mark.parametrize("url", ["https://user:secret@host", "https://host/path", "https://host?secret=yes", "http://host", "https://host:0"])
def test_service_origins_reject_ambiguous_or_credential_urls(url):
    with pytest.raises(ValueError):
        root_url(url)


def test_enabled_requires_pinned_image():
    with pytest.raises(ValueError, match="immutable"):
        WorkerConfig.from_env({"ROO_HERMES_ENABLED": "true", "ROO_HERMES_IMAGE": "hermes:latest"})


def test_container_does_not_inherit_parent_credentials_or_other_task_paths(tmp_path):
    cfg = config(tmp_path)
    launcher = DockerTaskLauncher(cfg)
    one, two = launcher.prepare(str(uuid4())), launcher.prepare(str(uuid4()))
    argv = launcher.command(one)
    text = " ".join(argv)
    assert "--read-only" in argv and "--cap-drop" in argv and "--security-opt" in argv
    assert cfg.worker_key not in text and "docker.sock" not in text and "--env-file" not in text
    assert str(two.root) not in text
    mounts = [argv[i + 1] for i, word in enumerate(argv) if word == "--mount"]
    assert len(mounts) == 3
    assert all(str(one.root) in mount for mount in mounts)
    assert mounts[-1].endswith("config.yaml,readonly")


@pytest.mark.asyncio
async def test_container_preflight_rejects_internet_network(tmp_path):
    launcher = DockerTaskLauncher(config(tmp_path))
    launcher._command = AsyncMock(return_value=b'[{"Internal": false}]')
    with pytest.raises(ValueError, match="internal"):
        await launcher.preflight()


@pytest.mark.asyncio
async def test_container_preflight_rejects_unrelated_peer_before_claim(tmp_path):
    launcher = DockerTaskLauncher(config(tmp_path))
    async def docker(*args):
        if args[:2] == ("network", "inspect"):
            return json.dumps([{"Internal": True, "Options": {"com.docker.network.bridge.name": "br-roo-hermes"},
                                "Containers": {"foreign-container": {}}}]).encode()
        return b'[{"Config": {"Labels": {"com.docker.compose.service": "admin-roo"}}}]'
    launcher._command = docker
    with pytest.raises(ValueError, match="unrelated"):
        await launcher.preflight()


@pytest.mark.asyncio
async def test_container_preflight_requires_verified_host_firewall(tmp_path):
    launcher = DockerTaskLauncher(config(tmp_path))
    async def docker(*args):
        bridge = "br-roo-hermes" if args[-1] == "roo-hermes-internal" else "br-roo-web"
        return json.dumps([{"Internal": True, "Options": {"com.docker.network.bridge.name": bridge}, "Containers": {}}]).encode()
    launcher._command = docker
    launcher._privileged_check = AsyncMock(return_value={"ok": True, "host_input_drop": False})
    with pytest.raises(ValueError, match="network isolation"):
        await launcher.preflight()


def test_public_web_still_routes_task_credentials_to_fixed_internal_broker(tmp_path):
    launcher = DockerTaskLauncher(replace(config(tmp_path), public_web_enabled=True))
    machine = launcher.prepare(str(uuid4()))
    command = launcher.command(machine, public_web=True)
    assert "NO_PROXY=hermes-broker,127.0.0.1,localhost" in command
    assert command[command.index("--log-driver") + 1] == "none"


def test_claim_contract_rejects_forged_identifiers_and_keeps_tokens_out_of_repr():
    sample = task()
    data = {**sample.__dict__, "inputs": []}
    parsed = ClaimedTask.parse(data)
    assert sample.tool_token not in repr(parsed) and sample.lease_token not in repr(parsed)
    with pytest.raises(BackendError):
        ClaimedTask.parse({**data, "task_id": "../../other-task"})


@pytest.mark.asyncio
async def test_backend_rejects_redirect_without_leaking_worker_key(tmp_path):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(307, headers={"location": "https://evil.invalid"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = BackendClient(config(tmp_path), transport)
        with pytest.raises(BackendError) as caught:
            await client.claim()
    assert caught.value.status == 307
    assert len(requests) == 1


class FakeLauncher(DockerTaskLauncher):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.stopped = False
    async def stop(self, machine):
        self.stopped = True
    async def limit(self, machine):
        pass


def client_with_task(sample):
    return SimpleNamespace(claim=AsyncMock(return_value=sample), heartbeat=AsyncMock(return_value={"status": "running"}),
                           complete=AsyncMock(), fail=AsyncMock())


@pytest.mark.asyncio
async def test_completion_stops_container_before_artifact_upload(tmp_path):
    cfg, sample = config(tmp_path), task()
    client, launcher = client_with_task(sample), FakeLauncher(cfg)
    class Runner:
        def __init__(self, *args, **kwargs):
            assert kwargs["collect_output_artifacts"] is False
        async def run(self, request):
            (request.workspace / "artifacts").mkdir()
            (request.workspace / "artifacts" / "report.md").write_text("A useful report")
            return HermesRunResult("completed", "Report ready")
    async def complete(*args, **kwargs):
        assert launcher.stopped
        assert kwargs["artifacts"][0].content == b"A useful report"
    client.complete.side_effect = complete
    result = await Worker(cfg, client, launcher, runner_factory=Runner).run_once()
    assert result["status"] == "completed"
    assert list(tmp_path.iterdir()) == []
    client.fail.assert_not_called()


@pytest.mark.asyncio
async def test_lease_loss_kills_execution_before_backend_failure(tmp_path):
    cfg, sample = replace(config(tmp_path), heartbeat_seconds=0.01), task()
    client, launcher = client_with_task(sample), FakeLauncher(cfg)
    client.heartbeat.side_effect = BackendError("revoked", status=403)
    was_cancelled = asyncio.Event()
    class Runner:
        def __init__(self, *args, **kwargs): pass
        async def run(self, request):
            try:
                await asyncio.Event().wait()
            finally:
                was_cancelled.set()
    async def fail(*args, **kwargs):
        assert was_cancelled.is_set() and launcher.stopped
    client.fail.side_effect = fail
    result = await Worker(cfg, client, launcher, runner_factory=Runner).run_once()
    assert result["status"] == "failed"
    client.complete.assert_not_called()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_symlink_artifact_never_uploads(tmp_path):
    cfg, sample = config(tmp_path), task()
    client, launcher = client_with_task(sample), FakeLauncher(cfg)
    class Runner:
        def __init__(self, *args, **kwargs): pass
        async def run(self, request):
            (request.workspace / "artifacts").mkdir()
            (request.workspace / "artifacts" / "stolen").symlink_to("/etc/passwd")
            return HermesRunResult("completed", "Done")
    result = await Worker(cfg, client, launcher, runner_factory=Runner).run_once()
    assert result["status"] == "failed"
    client.complete.assert_not_called()


@pytest.mark.asyncio
async def test_controller_cancellation_stops_and_does_not_publish(tmp_path):
    cfg, sample = config(tmp_path), task()
    client, launcher = client_with_task(sample), FakeLauncher(cfg)
    started = asyncio.Event()
    class Runner:
        def __init__(self, *args, **kwargs): pass
        async def run(self, request):
            started.set()
            await asyncio.Event().wait()
    execution = asyncio.create_task(Worker(cfg, client, launcher, runner_factory=Runner).run_once())
    await started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert launcher.stopped
    client.complete.assert_not_called()


@pytest.mark.asyncio
async def test_completion_retries_identical_body_after_lost_ack(tmp_path, monkeypatch):
    seen = []
    def handler(request):
        seen.append(request.content)
        if len(seen) == 1:
            raise httpx.ReadError("lost acknowledgement")
        return httpx.Response(200, json={"status": "completed"})
    monkeypatch.setattr("roo.proactive.client.asyncio.sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = BackendClient(config(tmp_path), transport)
        await client.complete(task(), result={"text": "done"}, artifacts=[])
    assert len(seen) == 2 and seen[0] == seen[1]


@pytest.mark.asyncio
async def test_rejection_after_uncertain_completion_stays_uncertain(tmp_path, monkeypatch):
    attempts = []
    def handler(request):
        attempts.append(request)
        return httpx.Response(503 if len(attempts) == 1 else 403)
    monkeypatch.setattr("roo.proactive.client.asyncio.sleep", AsyncMock())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        with pytest.raises(CompletionUnknown):
            await BackendClient(config(tmp_path), transport).complete(task(), result={"text": "done"}, artifacts=[])


class FinishingRunner:
    def __init__(self, *args, **kwargs): pass
    async def run(self, request):
        return HermesRunResult("completed", "Finished")


@pytest.mark.asyncio
async def test_uncertain_completion_retains_receipt_then_reconciles_without_rerun(tmp_path):
    cfg, sample = config(tmp_path), task()
    client, launcher = client_with_task(sample), FakeLauncher(cfg)
    client.complete.side_effect = CompletionUnknown()
    result = await Worker(cfg, client, launcher, runner_factory=FinishingRunner).run_once()
    assert result["status"] == "completion_unknown"
    client.fail.assert_not_called()
    roots = list(tmp_path.iterdir())
    assert len(roots) == 1
    receipt = roots[0] / "completion-pending.json"
    data = json.loads(receipt.read_text())
    assert data["payload"]["lease_token"] == sample.lease_token
    assert data["payload"]["result"]["text"] == "Finished"
    assert receipt.stat().st_mode & 0o077 == 0
    resolver = SimpleNamespace(post_completion=AsyncMock(return_value={"status": "completed"}))
    reconciled = await reconcile_pending(cfg, resolver, launcher)
    assert reconciled == {"status": "reconciled", "completions": 1}
    resolver.post_completion.assert_awaited_once_with(sample.task_id, data["payload"])
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("requested,expected", [(1, 1), (1800, 600)])
async def test_valid_backend_duration_is_clamped_to_controller_limit(tmp_path, requested, expected):
    sample = replace(task(), limits={"max_seconds": requested})
    client, launcher = client_with_task(sample), FakeLauncher(config(tmp_path))
    class Runner(FinishingRunner):
        async def run(self, request):
            assert request.timeout_seconds == expected
            assert request.task_id.startswith(sample.task_id + "-")
            return await super().run(request)
    result = await Worker(config(tmp_path), client, launcher, runner_factory=Runner).run_once()
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_preparation_failure_reports_failed_claim(tmp_path):
    sample, cfg = task(), config(tmp_path)
    client, launcher = client_with_task(sample), FakeLauncher(cfg)
    def broken_prepare(_):
        raise OSError("disk unavailable")
    launcher.prepare = broken_prepare
    result = await Worker(cfg, client, launcher, runner_factory=FinishingRunner).run_once()
    assert result["status"] == "failed"
    client.fail.assert_awaited_once()


@pytest.mark.asyncio
async def test_approval_required_is_visible_and_never_automatically_retried(tmp_path):
    sample, cfg = task(), config(tmp_path)
    client, launcher = client_with_task(sample), FakeLauncher(cfg)
    class Runner(FinishingRunner):
        async def run(self, request):
            return HermesRunResult("approval_required", permission_details=({"command": "restricted command", "reviewable": True},))
    result = await Worker(cfg, client, launcher, runner_factory=Runner).run_once()
    assert result["status"] == "approval_required"
    assert client.fail.call_args.kwargs["error_code"] == "approval_required"
    assert client.fail.call_args.kwargs["details"]["permission_requests"][0]["command"] == "restricted command"
    client.complete.assert_not_called()
