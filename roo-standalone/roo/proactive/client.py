"""Bounded controller-to-backend requests; no caller-selected API paths."""

from __future__ import annotations

import base64
import asyncio
from dataclasses import dataclass, field
import json
from uuid import UUID

import httpx

from .config import WorkerConfig


class BackendError(RuntimeError):
    def __init__(self, code="backend_unavailable", *, status=503):
        self.code, self.status = code, status
        super().__init__(code)


class CompletionUnknown(BackendError):
    """The completion may have committed; do not turn it into a failure."""

    def __init__(self):
        super().__init__("completion_unknown")


@dataclass(frozen=True)
class ClaimedTask:
    task_id: str
    lease_token: str = field(repr=False)
    lease_epoch: int
    instruction: str
    model: str
    tool_token: str = field(repr=False)
    model_token: str = field(repr=False)
    limits: dict = field(default_factory=dict)
    inputs: tuple = ()
    public_web: bool = False

    @classmethod
    def parse(cls, data):
        try:
            task_id = str(UUID(data["task_id"]))
            lease = str(UUID(data["lease_token"]))
            epoch = data["lease_epoch"]
            instruction = data["instruction"]
            model = data["model"]
            tool_token, model_token = data["tool_token"], data["model_token"]
            limits = data.get("limits", {})
            inputs = data.get("inputs", [])
            capabilities = data.get("capabilities", {})
            if (type(epoch) is not int or epoch < 1 or not isinstance(instruction, str)
                    or not instruction.strip() or len(instruction.encode()) > 100_000
                    or not isinstance(model, str) or not 1 <= len(model) <= 200
                    or not isinstance(limits, dict) or not isinstance(inputs, list)
                    or not isinstance(capabilities, dict) or set(capabilities) - {"public_web"}
                    or type(capabilities.get("public_web", False)) is not bool
                    or len(json.dumps(inputs)) > 100_000
                    or any(not isinstance(token, str) or not 32 <= len(token) <= 8192
                           or any(char.isspace() for char in token) for token in (tool_token, model_token))):
                raise ValueError()
            return cls(task_id, lease, epoch, instruction, model, tool_token, model_token, limits, tuple(inputs), capabilities.get("public_web", False))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BackendError("invalid_task_response", status=502) from None

    def lease(self):
        return {"lease_token": self.lease_token, "lease_epoch": self.lease_epoch}


class BackendClient:
    def __init__(self, config: WorkerConfig, client: httpx.AsyncClient):
        self.config, self.client = config, client
        self.base = config.backend_origin + "/api/v1/agent-runtime/internal"

    async def _post(self, suffix, payload):
        try:
            async with self.client.stream(
                "POST", self.base + suffix, json=payload,
                headers={"Authorization": "Bearer " + self.config.worker_key},
                follow_redirects=False, timeout=20,
            ) as response:
                if response.status_code == 204:
                    return None
                if response.status_code not in {200, 201, 202}:
                    raise BackendError("backend_rejected", status=response.status_code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1_048_576:
                        raise BackendError("backend_response_too_large", status=502)
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise BackendError("invalid_backend_response", status=502)
                return result
        except (httpx.HTTPError, ValueError):
            raise BackendError() from None

    async def claim(self):
        data = await self._post("/tasks/claim/", {"worker_id": self.config.worker_id})
        if not data or data.get("task") is None and "task_id" not in data:
            return None
        return ClaimedTask.parse(data.get("task", data))

    async def heartbeat(self, task):
        return await self._post(f"/tasks/{task.task_id}/heartbeat/", task.lease())

    @staticmethod
    def completion_payload(task, *, result, artifacts):
        return {
            **task.lease(), "result": result,
            "artifacts": [{"name": artifact.relative_path, "sha256": artifact.sha256,
                           "content_base64": base64.b64encode(artifact.content).decode("ascii")}
                          for artifact in artifacts],
        }

    async def complete(self, task, *, result, artifacts):
        payload = self.completion_payload(task, result=result, artifacts=artifacts)
        return await self.post_completion(task.task_id, payload)

    async def post_completion(self, task_id, payload):
        task_id = str(UUID(task_id))
        uncertain = False
        for attempt in range(3):
            try:
                return await self._post(f"/tasks/{task_id}/complete/", payload)
            except BackendError as exc:
                if exc.status >= 500 or exc.status in {408, 429}:
                    uncertain = True
                    if attempt < 2:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                if uncertain:
                    raise CompletionUnknown() from None
                raise


    async def fail(self, task, *, error_code, retryable=False, details=None):
        return await self._post(f"/tasks/{task.task_id}/fail/", {
            **task.lease(), "error_code": error_code, "retryable": retryable,
            **({"details": details} if details else {}),
        })
