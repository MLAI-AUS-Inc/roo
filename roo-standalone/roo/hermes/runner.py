"""Small ACP v1 client for the pinned, separately isolated Hermes worker.

The caller MUST create a separate container/VM per task and stop it after this
runner returns. Killing a docker/ssh client is not a container/remote-process
kill. This module never grants filesystem/terminal access through ACP callbacks.
"""

import asyncio
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import re
import signal
from typing import Sequence
from urllib.parse import urlsplit

from .artifacts import ArtifactError, collect_artifacts
from .types import HermesRunRequest, HermesRunResult


class ProtocolError(ValueError):
    pass


def permission_fingerprint(task_id: str, tool_call: dict) -> str:
    """Bind approval to task and exact command payload, not agent-chosen call ID.

    Only the pinned Hermes execute/rawInput shape is eligible. Its command and
    description are the bytes an operator must review. Each approval is consumed
    at most once; session/permanent approval options are never selected.
    """
    raw = tool_call.get("rawInput")
    if tool_call.get("kind") != "execute" or not isinstance(raw, dict):
        raise ProtocolError("permission_shape")
    if set(raw) != {"command", "description"} or not all(isinstance(v, str) for v in raw.values()):
        raise ProtocolError("permission_shape")
    payload = json.dumps([task_id, "execute", raw], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _endpoint(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment
            or any(c.isspace() for c in url)):
        raise ValueError("invalid trusted endpoint")
    return url


def _clean_path(path: Path) -> Path:
    path = Path(path).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("task paths must not contain symlinks")
    return path


def build_config(request: HermesRunRequest, runtime_workspace: str) -> dict:
    """Hermes accepts JSON as YAML. Config is controller-owned and mounted RO."""
    tools = ["terminal", "file", "todo"]
    if request.browser_enabled:
        tools.append("roo_browser_text")
    return {
        "model": {"provider": "custom", "default": request.model,
                  "base_url": request.model_proxy_url, "api_key": request.model_proxy_token,
                  "api_mode": "chat_completions"},
        "terminal": {"backend": "local", "cwd": runtime_workspace},
        "platform_toolsets": {"acp": tools},
        "agent": {"max_turns": request.max_turns,
                  "disabled_toolsets": ["cronjob", "delegation", "memory", "skills", "session_search"]},
        "approvals": {"mode": "manual", "cron_mode": "deny", "single_query_mode": "deny",
                      "unattended_mode": "deny", "timeout": 10},
        "skills": {"write_approval": True},
        "memory": {"memory_enabled": False, "user_profile_enabled": False, "write_approval": True},
        "browser": {"backend": "off", "use_real_profile": False, "headed": False,
                    "auto_local_for_private_urls": False, "allow_private_urls": False,
                    "extension_control": {"enabled": False}},
        "tool_loop_guardrails": {"hard_stop_enabled": True, "non_interactive_hard_stop_enabled": True},
        "auxiliary": {"title_generation": {"enabled": False, "model_upgrade_enabled": False},
                      "background_review": {"enabled": False},
                      "vision": {"provider": "main"}, "compression": {"provider": "main"},
                      "approval": {"provider": "main"}},
        "fallback_providers": [],
        "mcp_servers": {},
    }


class HermesRunner:
    def __init__(self, command: Sequence[str], *, trusted_broker_url: str,
                 trusted_model_proxy_url: str, runtime_workspace: str | None = None,
                 runtime_state_dir: str | None = None, termination_grace_seconds: float = 2,
                 collect_output_artifacts: bool = True):
        if not command or isinstance(command, str) or any(not isinstance(s, str) or "\0" in s for s in command):
            raise ValueError("command must be an argv sequence")
        self.command = tuple(command)
        self.broker_url = _endpoint(trusted_broker_url)
        self.model_url = _endpoint(trusted_model_proxy_url)
        self.runtime_workspace = runtime_workspace
        self.runtime_state_dir = runtime_state_dir
        self.grace = max(0.05, min(float(termination_grace_seconds), 10))
        self.collect_output_artifacts = collect_output_artifacts

    async def run(self, request: HermesRunRequest) -> HermesRunResult:
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request.task_id)
                or request.broker_url != self.broker_url or request.model_proxy_url != self.model_url
                or not 0 < request.timeout_seconds <= 3600 or not 0 < request.max_turns <= 100
                or not 0 < request.max_output_bytes <= 16_777_216
                or not 0 < request.max_artifact_bytes <= 33_554_432
                or not 0 < request.max_artifacts <= 100
                or not request.model or len(request.prompt.encode()) > 1_048_576
                or any(not t or len(t) > 8192 or any(c in t for c in "\r\n\0")
                       for t in (request.broker_token, request.model_proxy_token))):
            raise ValueError("invalid Hermes task configuration")
        workspace = _clean_path(request.workspace)
        state = _clean_path(request.state_dir)
        if workspace == state or workspace in state.parents or state in workspace.parents:
            raise ValueError("workspace and state must be separate")
        workspace.mkdir(parents=True, mode=0o700, exist_ok=True)
        state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if any(state.iterdir()):
            raise ValueError("Hermes task state must be fresh; use a new attempt directory")
        runtime_workspace = self.runtime_workspace or str(workspace)
        runtime_state = self.runtime_state_dir or str(state)
        if not runtime_workspace.startswith("/") or not runtime_state.startswith("/"):
            raise ValueError("runtime paths must be absolute")
        with (state / "config.yaml").open("x", encoding="utf-8") as stream:
            os.chmod(stream.name, 0o600)
            json.dump(build_config(request, runtime_workspace), stream)
        env = {
            "PATH": "/opt/hermes/bin:/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": runtime_state, "HERMES_HOME": runtime_state,
            "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONNOUSERSITE": "1",
            "HERMES_ACP_SKIP_CONFIGURED_MCP": "1", "HERMES_DISABLE_LAZY_INSTALLS": "1",
        }
        process = None
        drain_task = None
        session_id = None
        chunks: list[str] = []
        denied: list[str] = []
        permission_details: list[dict] = []
        used_permissions: set[str] = set()
        server_ids: set[str] = set()
        seen_responses: set[int] = set()
        pending_session_updates: list[dict] = []
        total = 0
        stderr_total = 0
        status = "failed"
        error_code = None
        next_id = 0

        async def send(message: dict) -> None:
            assert process and process.stdin
            process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            await process.stdin.drain()

        async def drain_stderr() -> None:
            nonlocal stderr_total
            assert process and process.stderr
            while data := await process.stderr.read(65536):
                stderr_total += len(data)
                # Discard diagnostics: they can contain prompts or credentials.
                if stderr_total > request.max_output_bytes:
                    raise ProtocolError("diagnostic_limit")

        async def response(method: str, params: dict) -> dict:
            nonlocal next_id, total
            next_id += 1
            expected = next_id
            await send({"jsonrpc": "2.0", "id": expected, "method": method, "params": params})
            while True:
                assert process and process.stdout
                if drain_task and drain_task.done():
                    drain_task.result()
                line_task = asyncio.create_task(process.stdout.readline())
                try:
                    done, _ = await asyncio.wait({line_task, drain_task}, return_when=asyncio.FIRST_COMPLETED)
                    if drain_task in done:
                        drain_task.result()
                    line = await line_task
                finally:
                    if not line_task.done():
                        line_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await line_task
                total += len(line)
                if not line or total > request.max_output_bytes:
                    raise ProtocolError("output_limit" if line else "unexpected_eof")
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProtocolError("invalid_json") from exc
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise ProtocolError("invalid_envelope")
                if "method" not in message:
                    if type(message.get("id")) is not int or message["id"] != expected or expected in seen_responses:
                        raise ProtocolError("unexpected_response")
                    seen_responses.add(expected)
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise ProtocolError("remote_error")
                    return message["result"]
                incoming = message.get("params", {})
                if not isinstance(incoming, dict):
                    raise ProtocolError("invalid_params")
                if "id" in message:
                    incoming_id = json.dumps(message["id"])
                    if incoming_id in server_ids:
                        raise ProtocolError("replayed_request")
                    server_ids.add(incoming_id)
                    if message["method"] == "session/request_permission":
                        fingerprint = "unrecognised_action"
                        allowed = False
                        if session_id and incoming.get("sessionId") == session_id:
                            try:
                                fingerprint = permission_fingerprint(request.task_id, incoming.get("toolCall", {}))
                                allowed = fingerprint in request.approved_permissions and fingerprint not in used_permissions
                            except (ProtocolError, AttributeError):
                                pass
                        options = incoming.get("options", [])
                        chosen = next((o.get("optionId") for o in options if isinstance(o, dict)
                                       and o.get("kind") == "allow_once" and isinstance(o.get("optionId"), str)), None)
                        outcome = {"outcome": "cancelled"}
                        if allowed and chosen:
                            used_permissions.add(fingerprint)
                            outcome = {"outcome": "selected", "optionId": chosen}
                        else:
                            denied.append(fingerprint)
                            raw = incoming.get("toolCall", {}).get("rawInput", {})
                            if fingerprint != "unrecognised_action" and isinstance(raw, dict):
                                detail = {"fingerprint": fingerprint}
                                for key in ("command", "description"):
                                    value = raw.get(key, "")
                                    for secret in (request.broker_token, request.model_proxy_token):
                                        value = value.replace(secret, "[redacted]")
                                    detail[key] = value[:8192]
                                detail["truncated"] = any(len(raw.get(k, "")) > 8192 for k in ("command", "description"))
                                # A truncated/redacted display cannot authorize
                                # unshown bytes; the reviewer must retrieve the
                                # original exact command through a separate gate.
                                detail["reviewable"] = not detail["truncated"] and all(detail[k] == raw.get(k, "") for k in ("command", "description"))
                                permission_details.append(detail)
                        await send({"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": outcome}})
                    else:
                        await send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Unsupported client method"}})
                elif message["method"] == "session/update":
                    if session_id is None and method == "session/new":
                        # Pinned Hermes schedules command/model notifications as
                        # it constructs the new-session response. Bind any early
                        # notification to the returned session before accepting it.
                        pending_session_updates.append(incoming)
                        continue
                    if not session_id or incoming.get("sessionId") != session_id:
                        raise ProtocolError("wrong_session")
                    update = incoming.get("update", {})
                    if not isinstance(update, dict):
                        raise ProtocolError("invalid_update")
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        content = update.get("content", {})
                        if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                            chunks.append(content["text"])

        try:
            async with asyncio.timeout(request.timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, cwd=workspace, env=env,
                    start_new_session=True, limit=min(request.max_output_bytes + 1, 1_048_576),
                )
                drain_task = asyncio.create_task(drain_stderr())
                initialized = await response("initialize", {"protocolVersion": 1, "clientInfo": {"name": "roo", "version": "1"}, "clientCapabilities": {}})
                capabilities = initialized.get("agentCapabilities", {})
                if initialized.get("protocolVersion") != 1 or not isinstance(capabilities, dict) or capabilities.get("mcpCapabilities", {}).get("http") is not True:
                    raise ProtocolError("unsupported_capabilities")
                created = await response("session/new", {"cwd": runtime_workspace, "mcpServers": [{
                    "type": "http", "name": "mlai-task", "url": request.broker_url,
                    "headers": [{"name": "Authorization", "value": "Bearer " + request.broker_token}],
                }]})
                session_id = created.get("sessionId")
                if not isinstance(session_id, str) or not 0 < len(session_id) <= 256:
                    raise ProtocolError("invalid_session")
                if any(update.get("sessionId") != session_id for update in pending_session_updates):
                    raise ProtocolError("wrong_session")
                task_prompt = ("Complete the assigned Roo task below. Write deliverable files only in "
                               "the artifacts subdirectory of the workspace.\n\n" + request.prompt)
                # Never let a member's leading /yolo, /model, /reset, etc. enter
                # Hermes' editor control-command parser as a protocol command.
                prompted = await response("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": task_prompt}]})
                stop = prompted.get("stopReason")
                if denied:
                    status = "approval_required"
                elif stop == "end_turn":
                    status = "completed"
                elif stop == "cancelled":
                    status = "cancelled"
                else:
                    error_code = "agent_" + stop if stop in {"refusal", "max_tokens", "max_turn_requests"} else "agent_incomplete"
        except TimeoutError:
            status, error_code = "timed_out", "deadline_exceeded"
        except asyncio.CancelledError:
            # Preserve cancellation semantics for the dispatcher's lease/grant cleanup.
            raise
        except ProtocolError as exc:
            error_code = str(exc)
        except (OSError, ValueError, TypeError, AttributeError, ConnectionError):
            error_code = "worker_protocol_failed"
        finally:
            if drain_task:
                drain_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await drain_task
            if process:
                if session_id and process.returncode is None:
                    with suppress(Exception):
                        await asyncio.wait_for(send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}}), timeout=0.2)
                await self._stop(process)
        text = "".join(chunks)
        for secret in (request.broker_token, request.model_proxy_token):
            text = text.replace(secret, "[redacted]")
        artifacts = ()
        if status == "completed" and self.collect_output_artifacts:
            try:
                artifacts = collect_artifacts(workspace, max_bytes=request.max_artifact_bytes,
                                              max_files=request.max_artifacts,
                                              secrets=(request.broker_token, request.model_proxy_token))
            except (ArtifactError, OSError):
                status, error_code = "failed", "unsafe_artifacts"
        return HermesRunResult(status, text, artifacts, session_id, error_code, tuple(denied), tuple(permission_details))

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        # A separate process group contains ordinary children; container cleanup
        # remains mandatory because detached or remote processes can escape it.
        async def discard(stream):
            if stream:
                while await stream.read(65536):
                    pass

        readers = [asyncio.create_task(discard(process.stdout)), asyncio.create_task(discard(process.stderr))]
        if process.stdin:
            process.stdin.close()
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=self.grace)
        except TimeoutError:
            pass
        finally:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=self.grace)
            try:
                await asyncio.wait_for(asyncio.gather(*readers), timeout=self.grace)
            except TimeoutError:
                # Detached children retaining pipes require the outer container
                # controller to kill them. Close local pipe transports too so an
                # escaped child cannot keep this client's event loop alive.
                transport = getattr(process, "_transport", None)
                if transport:
                    transport.close()
            finally:
                for reader in readers:
                    reader.cancel()
                await asyncio.gather(*readers, return_exceptions=True)
