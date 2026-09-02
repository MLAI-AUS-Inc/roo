"""
MLAI Backend Client

HTTP client for communicating with the mlai-backend service.
"""
import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from typing import Optional, Dict, Any, List, Union
from urllib.parse import quote, urlparse
from uuid import UUID, uuid4

import httpx

from ..backend_identity import (
    BackendActorContext,
    BackendIdentityError,
    build_org_memory_identity_headers,
    build_roo_gateway_identity_headers,
    build_victor_ai_identity_headers,
    get_backend_actor_context,
)
from ..config import get_settings

CONTENT_FACTORY_REQUEST_SOURCE = "roo_slackbot"
FULL_POINTS_ADMIN_ROLES = {"admin", "committee", "portfolio_lead"}


class MLAIBackendUnavailableError(RuntimeError):
    """Raised when Roo cannot reach mlai-backend reliably."""


class MLAIBackendClient:
    """Client for mlai-backend API."""

    _backend_transport_failures: Dict[str, int] = {}
    _slack_user_registration_cache: Dict[str, float] = {}
    _transport_failure_threshold = 3
    _slack_user_registration_ttl_seconds = 3600
    
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        internal_api_key: Optional[str] = None,
        service_principal_key: Optional[str] = None,
        victor_ai_signing_secret: Optional[str] = None,
        victor_ai_actor_context: Optional[dict] = None,
        surface: Optional[str] = None,
        actor_context: Optional[BackendActorContext] = None,
    ):
        settings = None
        if base_url is None or (
            api_key is None
            and internal_api_key is None
            and service_principal_key is None
            and victor_ai_signing_secret is None
        ):
            settings = get_settings()

        self.base_url = base_url or (settings.MLAI_BACKEND_URL if settings else None)
        self.roo_api_key = api_key or (
            settings.ROO_API_KEY if settings else None
        )
        self.api_key = self.roo_api_key or (
            settings.MLAI_API_KEY if settings else None
        )
        self.internal_api_key = (
            internal_api_key
            or (settings.INTERNAL_API_KEY if settings else None)
            or (settings.ROO_API_KEY if settings else None)
            or (settings.MLAI_API_KEY if settings else None)
        )
        self.service_principal_key = service_principal_key or (
            getattr(settings, "ORG_BRAIN_API_KEY", None) if settings else None
        )
        self.victor_ai_signing_secret = victor_ai_signing_secret or (
            getattr(settings, "VICTOR_AI_ROO_SIGNING_SECRET", None) if settings else None
        )
        self.victor_ai_actor_context = dict(victor_ai_actor_context or {})
        self.surface = surface or (
            getattr(settings, "ROO_SURFACE", "public") if settings else "public"
        )
        self.actor_context = actor_context or get_backend_actor_context()
        self.base_url = self.base_url.rstrip('/') if self.base_url else ""
        self._points_base = "/api/v1/points"
        self._data_base = "/api/v1/data"
        self._admin_cache: Dict[str, bool] = {}

    def _backend_key(self) -> str:
        return self.base_url or "unconfigured"

    def _new_request_id(self) -> str:
        return f"roo-{uuid4().hex}"

    def _circuit_breaker_is_open(self) -> bool:
        backend_key = self._backend_key()
        failures = self._backend_transport_failures.get(backend_key, 0)
        return failures >= self._transport_failure_threshold

    def _log_mlai_request(
        self,
        *,
        method: str,
        endpoint: str,
        attempt: int,
        total_attempts: int,
        timeout: float,
        request_id: str,
        circuit_open: bool,
        circuit_breaker: bool,
    ) -> None:
        print(
            "🌐 MLAI request "
            f"method={method.upper()} endpoint={endpoint} "
            f"attempt={attempt}/{total_attempts} timeout_bucket={timeout}s request_id={request_id} "
            f"circuit_breaker={circuit_breaker} circuit_open={circuit_open}"
        )

    def _log_mlai_transport_error(
        self,
        *,
        method: str,
        endpoint: str,
        attempt: int,
        total_attempts: int,
        timeout: float,
        request_id: str,
        exc: Exception,
        duration_ms: float,
        circuit_open: bool,
        circuit_breaker: bool,
        redact_logs: bool = False,
    ) -> None:
        exception_detail = (
            f"exc_type={exc.__class__.__name__}"
            if redact_logs
            else f"exc_type={exc.__class__.__name__} exc_repr={exc!r}"
        )
        print(
            "🌐 MLAI request_failed "
            f"method={method.upper()} endpoint={endpoint} "
            f"attempt={attempt}/{total_attempts} timeout_bucket={timeout}s request_id={request_id} "
            f"duration_ms={duration_ms:.2f} "
            f"{exception_detail} "
            f"circuit_breaker={circuit_breaker} circuit_open={circuit_open}"
        )

    def _log_mlai_response(
        self,
        *,
        method: str,
        endpoint: str,
        attempt: int,
        total_attempts: int,
        timeout: float,
        request_id: str,
        status_code: int,
        duration_ms: float,
        circuit_open: bool,
        circuit_breaker: bool,
    ) -> None:
        print(
            "🌐 MLAI response "
            f"method={method.upper()} endpoint={endpoint} "
            f"attempt={attempt}/{total_attempts} timeout_bucket={timeout}s request_id={request_id} "
            f"status={status_code} duration_ms={duration_ms:.2f} "
            f"circuit_breaker={circuit_breaker} circuit_open={circuit_open}"
        )

    @classmethod
    def _registration_cache_key(cls, slack_id: str, email: str) -> str:
        return f"{str(slack_id).strip()}::{str(email).strip().lower()}"

    @classmethod
    def _prune_registration_cache(cls) -> None:
        now = time.monotonic()
        ttl = cls._slack_user_registration_ttl_seconds
        for cache_key, recorded_at in list(cls._slack_user_registration_cache.items()):
            if now - recorded_at >= ttl:
                cls._slack_user_registration_cache.pop(cache_key, None)

    @classmethod
    def _is_registration_cached(cls, slack_id: str, email: str) -> bool:
        cls._prune_registration_cache()
        cache_key = cls._registration_cache_key(slack_id, email)
        recorded_at = cls._slack_user_registration_cache.get(cache_key)
        if recorded_at is None:
            return False
        return (time.monotonic() - recorded_at) < cls._slack_user_registration_ttl_seconds

    @classmethod
    def _mark_registration_cached(cls, slack_id: str, email: str) -> None:
        cls._prune_registration_cache()
        cls._slack_user_registration_cache[cls._registration_cache_key(slack_id, email)] = time.monotonic()

    @classmethod
    def _record_transport_success(cls, backend_key: str) -> None:
        cls._backend_transport_failures[backend_key] = 0

    @classmethod
    def _record_transport_failure(cls, backend_key: str) -> int:
        failures = cls._backend_transport_failures.get(backend_key, 0) + 1
        cls._backend_transport_failures[backend_key] = failures
        return failures

    async def _probe_backend_readiness(self) -> bool:
        if not self.base_url:
            return False

        request_id = self._new_request_id()
        backend_key = self._backend_key()
        previous_failures = self._backend_transport_failures.get(backend_key, 0)
        try:
            response = await self._request(
                "GET",
                "/healthz/ready",
                timeout=5.0,
                request_id=request_id,
                transport_retries=1,
                retry_backoff_seconds=0.25,
                circuit_breaker=False,
                use_admin_headers=False,
            )
            response.raise_for_status()
        except Exception as exc:
            if previous_failures:
                self._backend_transport_failures[backend_key] = previous_failures
            print(
                "🌐 MLAI readiness_probe_failed "
                f"endpoint=/healthz/ready request_id={request_id} "
                f"exc_type={exc.__class__.__name__} exc_repr={exc!r}"
            )
            return False

        print(
            "🌐 MLAI readiness_probe_ok "
            f"endpoint=/healthz/ready request_id={request_id} status={response.status_code}"
        )
        return True

    async def _guard_circuit_breaker(self, endpoint: str) -> None:
        backend_key = self._backend_key()
        failures = self._backend_transport_failures.get(backend_key, 0)
        if failures < self._transport_failure_threshold:
            return

        print(
            "🌐 MLAI circuit_breaker_open "
            f"endpoint={endpoint} consecutive_failures={failures} base_url={self.base_url}"
        )
        if await self._probe_backend_readiness():
            self._record_transport_success(backend_key)
            return

        raise MLAIBackendUnavailableError(
            "MLAI backend is unavailable right now."
        )

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[dict] = None,
        json: Optional[dict] = None,
        headers: Optional[dict] = None,
        timeout: float = 10.0,
        request_id: Optional[str] = None,
        transport_retries: int = 0,
        retry_backoff_seconds: float = 0.25,
        circuit_breaker: bool = False,
        use_admin_headers: bool = False,
        use_org_memory_identity: bool = False,
        use_victor_ai_identity: bool = False,
        redact_logs: bool = False,
    ) -> httpx.Response:
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        normalized_endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        if circuit_breaker and normalized_endpoint != "/healthz/ready":
            await self._guard_circuit_breaker(normalized_endpoint)

        request_id = request_id or self._new_request_id()
        if use_org_memory_identity and use_victor_ai_identity:
            raise BackendIdentityError(
                "A backend request cannot use two actor assertion types"
            )
        if use_org_memory_identity:
            resolved_headers = self.org_memory_headers(request_id)
        elif use_victor_ai_identity:
            resolved_headers = self.victor_ai_headers(request_id)
        else:
            resolved_headers = dict(self.admin_headers if use_admin_headers else self.headers)
        if headers:
            resolved_headers.update(headers)
        resolved_headers["X-Request-ID"] = request_id

        total_attempts = max(1, int(transport_retries) + 1)
        backend_key = self._backend_key()

        for attempt in range(1, total_attempts + 1):
            circuit_open = self._circuit_breaker_is_open()
            self._log_mlai_request(
                method=method,
                endpoint=normalized_endpoint,
                attempt=attempt,
                total_attempts=total_attempts,
                timeout=timeout,
                request_id=request_id,
                circuit_open=circuit_open,
                circuit_breaker=circuit_breaker,
            )
            started_at = time.monotonic()
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.request(
                        method.upper(),
                        f"{self.base_url}{normalized_endpoint}",
                        params=params,
                        json=json,
                        headers=resolved_headers,
                        timeout=timeout,
                    )
            except httpx.TransportError as exc:
                duration_ms = (time.monotonic() - started_at) * 1000
                failures = self._record_transport_failure(backend_key)
                self._log_mlai_transport_error(
                    method=method,
                    endpoint=normalized_endpoint,
                    attempt=attempt,
                    total_attempts=total_attempts,
                    timeout=timeout,
                    request_id=request_id,
                    exc=exc,
                    duration_ms=duration_ms,
                    circuit_open=circuit_open,
                    circuit_breaker=circuit_breaker,
                    redact_logs=redact_logs,
                )
                if attempt < total_attempts:
                    await asyncio.sleep(retry_backoff_seconds * (2 ** (attempt - 1)))
                    continue
                raise MLAIBackendUnavailableError(
                    f"MLAI backend transport failure for {normalized_endpoint} after {failures} consecutive failures."
                ) from exc

            self._record_transport_success(backend_key)
            duration_ms = (time.monotonic() - started_at) * 1000
            self._log_mlai_response(
                method=method,
                endpoint=normalized_endpoint,
                attempt=attempt,
                total_attempts=total_attempts,
                timeout=timeout,
                request_id=request_id,
                status_code=response.status_code,
                duration_ms=duration_ms,
                circuit_open=circuit_open,
                circuit_breaker=circuit_breaker,
            )
            return response

        raise MLAIBackendUnavailableError(
            f"MLAI backend transport failure for {normalized_endpoint}."
        )

    def _clean_slack_id(self, user_id: str) -> str:
        """Clean a Slack ID or mention string to extract the ID."""
        if not user_id:
            return user_id
        # Handle <@U12345> format
        if user_id.startswith("<@") and user_id.endswith(">"):
            parts = user_id[2:-1].split("|")
            return parts[0]
        # Handle @U12345 format
        if user_id.startswith("@"):
            return user_id[1:]
        return user_id

    @property
    def headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    @property
    def admin_headers(self) -> dict:
        """Headers for admin endpoints using internal secure key."""
        headers = {"Content-Type": "application/json"}
        # Prefer the internal key, but fall back to the standard API key so
        # admin endpoints still authenticate when only one key is configured.
        if hasattr(self, 'internal_api_key') and self.internal_api_key:
             headers["X-API-Key"] = self.internal_api_key
        elif self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def org_memory_headers(self, request_id: str) -> dict:
        """Build a one-request Admin Roo identity assertion for private memory."""
        if self.surface != "admin":
            raise BackendIdentityError(
                "Only Admin Roo can call organisational-memory endpoints"
            )
        if not self.service_principal_key:
            raise BackendIdentityError("ORG_BRAIN_API_KEY is not configured")
        if not self.actor_context:
            raise BackendIdentityError(
                "No verified Slack actor context is active"
            )
        return build_org_memory_identity_headers(
            self.service_principal_key,
            context=self.actor_context,
            request_id=request_id,
        )

    def roo_gateway_headers(self, request_id: str) -> dict:
        """Build a route-only actor assertion that cannot retrieve memory."""
        if self.surface not in {"gateway", "roo_gateway"}:
            raise BackendIdentityError(
                "Only the Roo gateway can call routing eligibility"
            )
        if not self.service_principal_key:
            raise BackendIdentityError("ORG_BRAIN_ROUTER_API_KEY is not configured")
        if not self.actor_context:
            raise BackendIdentityError("No verified Slack actor context is active")
        return build_roo_gateway_identity_headers(
            self.service_principal_key,
            context=self.actor_context,
            request_id=request_id,
        )

    def victor_ai_headers(self, request_id: str) -> dict:
        """Build a fresh signed Slack actor assertion for one Victor read."""

        secret = str(self.victor_ai_signing_secret or "")
        if len(secret) < 32:
            raise ValueError("VICTOR_AI_ROO_SIGNING_SECRET is not configured")

        context = self.actor_context
        if context is None and self.victor_ai_actor_context:
            context = BackendActorContext(
                slack_team_id=str(
                    self.victor_ai_actor_context.get("slack_team_id") or ""
                ).strip(),
                acting_slack_user_id=str(
                    self.victor_ai_actor_context.get(
                        "acting_slack_user_id"
                    )
                    or ""
                ).strip(),
                slack_channel_id=str(
                    self.victor_ai_actor_context.get("slack_channel_id") or ""
                ).strip(),
                slack_thread_ts=str(
                    self.victor_ai_actor_context.get("slack_thread_ts") or ""
                ).strip(),
                event_id=str(
                    self.victor_ai_actor_context.get("event_id") or ""
                ).strip(),
            )
        if context is None:
            raise ValueError("Verified Slack context is unavailable for this Victor request")
        return build_victor_ai_identity_headers(
            secret,
            context=context,
            request_id=request_id,
        )

    def _victor_ai_endpoint(self, suffix: str) -> str:
        """Support backend base URLs with or without an /api/v1 suffix."""

        raw_suffix = str(suffix or "")
        suffix = raw_suffix.strip("/")
        base_path = urlparse(self.base_url).path.rstrip("/")
        prefix = "" if base_path.endswith("/api/v1") else "/api/v1"
        root = f"{prefix}/victor-ai/roo/applications/"
        if not suffix:
            return root
        trailing_slash = "/" if raw_suffix.endswith("/") else ""
        return f"{root}{suffix}{trailing_slash}"

    def _org_memory_endpoint(self, suffix: str) -> str:
        """Support backend base URLs with or without an /api/v1 suffix."""

        suffix = str(suffix or "").strip("/")
        base_path = urlparse(self.base_url).path.rstrip("/")
        prefix = "" if base_path.endswith("/api/v1") else "/api/v1"
        return f"{prefix}/org-memory/{suffix}"

    @staticmethod
    def _victor_ai_params(filters: Optional[dict] = None, **pagination: Any) -> dict:
        allowed = {
            "stage",
            "role",
            "startup_stage",
            "industry_sector",
            "created_after",
            "created_before",
            "q",
        }
        params = {
            key: value
            for key, value in (filters or {}).items()
            if key in allowed and value not in (None, "")
        }
        params.update(
            {
                key: value
                for key, value in pagination.items()
                if value not in (None, "")
            }
        )
        return params

    async def get_victor_application_summary(
        self,
        *,
        filters: Optional[dict] = None,
        timeout: float = 20.0,
    ) -> dict:
        response = await self._request(
            "GET",
            self._victor_ai_endpoint("summary/"),
            params=self._victor_ai_params(filters),
            timeout=timeout,
            circuit_breaker=True,
            use_victor_ai_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def list_victor_applications(
        self,
        *,
        filters: Optional[dict] = None,
        limit: int = 10,
        offset: int = 0,
        timeout: float = 20.0,
    ) -> dict:
        response = await self._request(
            "GET",
            self._victor_ai_endpoint(""),
            params=self._victor_ai_params(filters, limit=limit, offset=offset),
            timeout=timeout,
            circuit_breaker=True,
            use_victor_ai_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_victor_application(
        self,
        application_id: int,
        *,
        timeout: float = 20.0,
    ) -> dict:
        response = await self._request(
            "GET",
            self._victor_ai_endpoint(f"{int(application_id)}/"),
            timeout=timeout,
            circuit_breaker=True,
            use_victor_ai_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def export_victor_applications_csv(
        self,
        *,
        filters: Optional[dict] = None,
        timeout: float = 20.0,
    ) -> tuple[str, str]:
        response = await self._request(
            "GET",
            self._victor_ai_endpoint("export.csv"),
            params=self._victor_ai_params(filters),
            timeout=timeout,
            circuit_breaker=True,
            use_victor_ai_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        disposition = str(response.headers.get("Content-Disposition") or "")
        match = re.search(r'filename="?([^";]+)', disposition, flags=re.IGNORECASE)
        filename = match.group(1).strip() if match else "victor-ai-applications.csv"
        return response.text, filename

    async def get_org_memory_actor_context(self) -> dict:
        """Validate the scoped service identity without retrieving memory data."""
        response = await self._request(
            "GET",
            self._org_memory_endpoint("auth/context"),
            timeout=10.0,
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_org_memory_pilot_access_probe(self) -> dict:
        """Prove the live pilot boundary without retrieving memory data."""
        response = await self._request(
            "GET",
            self._org_memory_endpoint("pilot/access-check"),
            timeout=10.0,
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_admin_routing_eligibility(self) -> dict:
        """Return the backend-owned, content-free Admin Brain route decision."""
        request_id = self._new_request_id()
        response = await self._request(
            "POST",
            self._org_memory_endpoint("routing/eligibility"),
            headers=self.roo_gateway_headers(request_id),
            json={},
            timeout=10.0,
            request_id=request_id,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def answer_org_memory(
        self,
        query: str,
        *,
        channel_id: Optional[str] = None,
        thread_ts: Optional[str] = None,
        answer_mode: str = "auto",
        as_of: Optional[str] = None,
        time_start: Optional[str] = None,
        time_end: Optional[str] = None,
        max_context_tokens: int = 6000,
        timeout: float = 20.0,
    ) -> dict:
        payload: dict[str, Any] = {
            "query": str(query or "").strip(),
            "answer_mode": str(answer_mode or "auto").strip().lower(),
            "max_context_tokens": int(max_context_tokens),
        }
        if channel_id:
            payload["channel_id"] = str(channel_id)
        if thread_ts:
            payload["thread_ts"] = str(thread_ts)
        if as_of:
            payload["as_of"] = str(as_of)
        if time_start or time_end:
            payload["time_range"] = {
                "start": str(time_start) if time_start else None,
                "end": str(time_end) if time_end else None,
            }
        response = await self._request(
            "POST",
            self._org_memory_endpoint("answer"),
            json=payload,
            timeout=float(timeout),
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_org_memory_query_trace(
        self,
        query_id: str,
        *,
        timeout: float = 20.0,
    ) -> dict:
        response = await self._request(
            "GET",
            self._org_memory_endpoint(
                f"queries/{str(query_id).strip()}/trace"
            ),
            timeout=float(timeout),
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def submit_org_memory_feedback(
        self,
        *,
        query_id: str,
        feedback_type: str,
        claim_id: Optional[str] = None,
        correction_text: Optional[str] = None,
        timeout: float = 20.0,
    ) -> dict:
        payload: dict[str, Any] = {
            "query_id": str(query_id).strip(),
            "feedback_type": str(feedback_type).strip().lower(),
        }
        if claim_id:
            payload["claim_id"] = str(claim_id).strip()
        if correction_text:
            payload["correction_text"] = str(correction_text).strip()
        response = await self._request(
            "POST",
            self._org_memory_endpoint("feedback"),
            json=payload,
            timeout=float(timeout),
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    @staticmethod
    def _org_memory_action_id(value: Any) -> str:
        try:
            return str(UUID(str(value or "").strip()))
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(
                "A valid controlled-action proposal ID is required"
            ) from exc

    @staticmethod
    def _org_memory_idempotency_key(value: Any) -> str:
        key = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,255}", key):
            raise ValueError(
                "A controlled-action idempotency key must contain "
                "8-255 safe characters"
            )
        return key

    async def list_org_memory_actions(
        self,
        *,
        status: Optional[str] = None,
        limit: int = 20,
        timeout: float = 20.0,
    ) -> dict:
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 200))}
        if status:
            params["status"] = str(status).strip()
        response = await self._request(
            "GET",
            self._org_memory_endpoint("actions"),
            params=params,
            timeout=float(timeout),
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_org_memory_action(
        self,
        proposal_id: str,
        *,
        timeout: float = 20.0,
    ) -> dict:
        action_id = self._org_memory_action_id(proposal_id)
        response = await self._request(
            "GET",
            self._org_memory_endpoint(f"actions/{action_id}"),
            timeout=float(timeout),
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def create_org_memory_action(
        self,
        *,
        action_type: str,
        input_payload: dict,
        idempotency_key: str,
        configuration_id: Optional[str] = None,
        evidence_claim_ids: Optional[list[str]] = None,
        evidence_source_ids: Optional[list[str]] = None,
        timeout: float = 20.0,
    ) -> dict:
        payload: dict[str, Any] = {
            "action_type": str(action_type or "").strip(),
            "input_payload": dict(input_payload or {}),
            "evidence_claim_ids": list(evidence_claim_ids or []),
            "evidence_source_ids": list(evidence_source_ids or []),
        }
        if configuration_id:
            payload["configuration_id"] = str(configuration_id).strip()
        response = await self._request(
            "POST",
            self._org_memory_endpoint("actions"),
            json=payload,
            headers={
                "Idempotency-Key": self._org_memory_idempotency_key(
                    idempotency_key
                )
            },
            timeout=float(timeout),
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def approve_org_memory_action(
        self,
        proposal_id: str,
        *,
        idempotency_key: str,
        timeout: float = 20.0,
    ) -> dict:
        return await self._transition_org_memory_action(
            proposal_id,
            transition="approve",
            payload={},
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    async def reject_org_memory_action(
        self,
        proposal_id: str,
        *,
        reason: str,
        idempotency_key: str,
        timeout: float = 20.0,
    ) -> dict:
        return await self._transition_org_memory_action(
            proposal_id,
            transition="reject",
            payload={"reason": str(reason or "").strip()},
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    async def execute_org_memory_action(
        self,
        proposal_id: str,
        *,
        idempotency_key: str,
        timeout: float = 20.0,
    ) -> dict:
        return await self._transition_org_memory_action(
            proposal_id,
            transition="execute",
            payload={},
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    async def reverse_org_memory_action(
        self,
        proposal_id: str,
        *,
        idempotency_key: str,
        timeout: float = 20.0,
    ) -> dict:
        return await self._transition_org_memory_action(
            proposal_id,
            transition="reverse",
            payload={"confirm": True},
            idempotency_key=idempotency_key,
            timeout=timeout,
        )

    async def _transition_org_memory_action(
        self,
        proposal_id: str,
        *,
        transition: str,
        payload: dict,
        idempotency_key: str,
        timeout: float,
    ) -> dict:
        action_id = self._org_memory_action_id(proposal_id)
        if transition not in {"approve", "reject", "execute", "reverse"}:
            raise ValueError("Unsupported controlled-action transition")
        response = await self._request(
            "POST",
            self._org_memory_endpoint(
                f"actions/{action_id}/{transition}"
            ),
            json=dict(payload or {}),
            headers={
                "Idempotency-Key": self._org_memory_idempotency_key(
                    idempotency_key
                )
            },
            timeout=float(timeout),
            circuit_breaker=True,
            use_org_memory_identity=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    def _extract_response_body(self, response: httpx.Response) -> str:
        """Return a compact, log-friendly response body."""
        try:
            payload = response.json()
        except ValueError:
            payload = response.text.strip()

        if payload in (None, "", [], {}):
            return ""
        return repr(payload)

    @staticmethod
    def _describe_exception(exc: Exception) -> str:
        detail = str(exc).strip()
        return detail or exc.__class__.__name__

    def _raise_for_status_or_backend_unavailable(self, response: httpx.Response) -> None:
        if response.status_code == 503:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            message = str(
                payload.get("message")
                or payload.get("error")
                or payload.get("detail")
                or "MLAI backend is unavailable right now."
            ).strip()
            raise MLAIBackendUnavailableError(message)
        response.raise_for_status()

    def _log_points_request_step(
        self,
        *,
        step: str,
        endpoint: str,
        status: Any,
        error_body: str = "",
    ) -> None:
        """Emit consistent logs for points-request API calls."""
        suffix = f" error_body={error_body}" if error_body else ""
        print(
            f"🧾 Points request step={step} endpoint={endpoint} "
            f"status={status}{suffix}"
        )

    def _log_points_request_transport_error(
        self,
        *,
        step: str,
        endpoint: str,
        exc: Exception,
    ) -> None:
        """Log network/transport failures for points-request API calls."""
        error_body = f"{type(exc).__name__}: {exc!r}"
        print(
            f"🧾 Points request step={step} endpoint={endpoint} "
            f"status=transport_error error_body={error_body}"
        )
    
    async def save_article_generation(
        self,
        slack_user_id: str,
        job_id: str,
        domain: str,
        result: Optional[dict] = None
    ) -> dict:
        """
        Save article generation record to mlai-backend.
        
        Args:
            slack_user_id: Slack user who triggered generation
            job_id: Content Factory job ID
            domain: Target domain
            result: Optional generation result data
        
        Returns:
            Created record data
        """
        if not self.base_url:
            print("⚠️  MLAI_BACKEND_URL not configured, skipping save")
            return {}
        
        payload = {
            "slack_user_id": slack_user_id,
            "job_id": job_id,
            "domain": domain,
        }
        
        if result:
            payload.update({
                "topic": result.get("topic"),
                "title": result.get("title"),
                "slug": result.get("slug"),
                "meta_title": result.get("meta_title"),
                "meta_description": result.get("meta_description"),
                "keywords": result.get("keywords", []),
                "status": "completed"
            })
        
        response = await self._request(
            "POST",
            "/api/roo/article-generations/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()
    
    async def get_user_by_slack_id(self, slack_id: str) -> Optional[Dict[str, Any]]:
        """
        Get user information by Slack ID.
        
        Args:
            slack_id: Slack user ID
        
        Returns:
            User data or None if not found
        """
        if not self.base_url:
            return None
        
        try:
            response = await self._request(
                "GET",
                f"/api/roo/users/slack/{slack_id}/",
                timeout=10.0,
                circuit_breaker=True,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"❌ User lookup failed: {e}")
            return None
    
    async def create_user(
        self,
        slack_id: str,
        name: str,
        email: Optional[str] = None
    ) -> dict:
        """Create a new user in mlai-backend (legacy endpoint)."""
        if not self.base_url:
            return {}

        response = await self._request(
            "POST",
            "/api/roo/users/",
            json={
                "slack_id": slack_id,
                "name": name,
                "email": email
            },
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def ensure_slack_user_registered(
        self,
        slack_id: str,
        email: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        avatar_url: Optional[str] = None
    ) -> dict:
        """
        Ensure a Slack user is registered in mlai-backend.
        This is the recommended endpoint for user registration.

        Uses the /api/v1/users/slack-user/ endpoint which:
        - Creates new user if slack_id doesn't exist
        - Returns existing user if already registered
        - Links slack_id to existing email if found

        Args:
            slack_id: Slack user ID (e.g., "U05QPB483K9")
            email: User's email address (required)
            first_name: User's first name (optional)
            last_name: User's last name (optional)
            avatar_url: URL to user's Slack avatar (optional)

        Returns:
            Dict with keys: user_id, email, slack_id, created (bool)

        Raises:
            httpx.HTTPError: If API request fails
        """
        if not self.base_url:
            return {}

        clean_slack_id = self._clean_slack_id(slack_id)
        normalized_email = str(email or "").strip().lower()
        if self._is_registration_cached(clean_slack_id, normalized_email):
            return {
                "slack_id": clean_slack_id,
                "email": normalized_email,
                "created": False,
                "cached": True,
            }

        payload = {
            "slack_id": clean_slack_id,
            "email": normalized_email,
        }

        if first_name:
            payload["first_name"] = first_name
        if last_name:
            payload["last_name"] = last_name
        if avatar_url:
            payload["avatar_url"] = avatar_url

        response = await self._request(
            "POST",
            "/api/v1/users/slack-user/",
            json=payload,
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        data = response.json()
        self._mark_registration_cached(clean_slack_id, normalized_email)
        return data

    async def trigger_article_generation(
        self,
        slack_user_id: str,
        domain: str,
        topic: Optional[str] = None,
        target_keyword: Optional[str] = None,
        context: Optional[str] = None,
        delivery_mode: Optional[str] = None,
        delivery_mode_confirmed: Optional[bool] = None,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        progress_message_ts: Optional[str] = None,
        client_request_id: Optional[str] = None,
        request_source: str = CONTENT_FACTORY_REQUEST_SOURCE,
        user_email: Optional[str] = None,
        user_first_name: Optional[str] = None,
        user_last_name: Optional[str] = None,
        user_avatar_url: Optional[str] = None,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """
        Trigger article generation via mlai-backend.

        Args:
            slack_user_id: Slack ID of requesting user
            domain: Target domain
            topic: Article topic (Optional - if omitted, triggers auto-research)
            target_keyword: Main keyword
            context: Optional conversation context
            slack_channel_id: Slack channel for in-thread replies
            slack_thread_ts: Slack thread timestamp for in-thread replies

        Returns:
            Dict containing job_id and status
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        payload = {
            "slack_user_id": slack_user_id,
            "domain": domain,
            "context": context,
            "request_source": request_source,
        }
        if requested_by_slack_user_id:
            payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)

        # Only add topic/keyword if provided (omitting them triggers auto-research)
        if topic:
            payload["topic"] = topic
        if target_keyword:
            payload["target_keyword"] = target_keyword
        if delivery_mode is not None:
            payload["delivery_mode"] = delivery_mode
            payload["delivery_mode_confirmed"] = bool(delivery_mode_confirmed)
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts
        if progress_message_ts:
            payload["progress_message_ts"] = progress_message_ts
        if client_request_id:
            payload["client_request_id"] = client_request_id
        if user_email:
            payload["user_email"] = user_email
        if user_first_name:
            payload["user_first_name"] = user_first_name
        if user_last_name:
            payload["user_last_name"] = user_last_name
        if user_avatar_url:
            payload["user_avatar_url"] = user_avatar_url

        response = await self._request(
            "POST",
            "/api/v1/content/generate",
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def attach_content_progress_message(
        self,
        job_id: str,
        *,
        progress_message_ts: str,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        slack_root_message_ts: Optional[str] = None,
        request_source: str = CONTENT_FACTORY_REQUEST_SOURCE,
    ) -> dict:
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        payload = {
            "progress_message_ts": progress_message_ts,
            "request_source": request_source,
        }
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts
        if slack_root_message_ts:
            payload["slack_root_message_ts"] = slack_root_message_ts

        response = await self._request(
            "POST",
            f"/api/v1/content/jobs/{job_id}/progress-message",
            json=payload,
            timeout=15.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def maybe_send_content_still_working(
        self,
        job_id: str,
        *,
        request_source: str = CONTENT_FACTORY_REQUEST_SOURCE,
    ) -> dict:
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        response = await self._request(
            "POST",
            f"/api/v1/content/jobs/{job_id}/still-working",
            json={"request_source": request_source},
            timeout=15.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def confirm_article_topic(
        self,
        job_id: str,
        slack_user_id: str,
        domain: Optional[str] = None,
        confirmed_keyword: Optional[str] = None,
        custom_title: Optional[str] = None,
        option_index: int = 0,
        delivery_mode: Optional[str] = None,
        delivery_mode_confirmed: Optional[bool] = None,
        request_source: str = CONTENT_FACTORY_REQUEST_SOURCE,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """
        Confirm topic selection for article generation.

        Args:
            job_id: The ID of the research job
            slack_user_id: The Slack user confirming the topic
            domain: The target domain (optional - backend gets from job if not provided)
            confirmed_keyword: The selected/confirmed keyword (optional - backend gets from job if not provided)
            custom_title: Optional custom title override
            option_index: The index of the selected option (default 0)

        Returns:
            Response from backend
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        # Build payload - only include fields that are provided
        payload = {
            "action": "write",
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "option_index": option_index,
            "request_source": request_source,
        }
        if requested_by_slack_user_id:
            payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)

        if confirmed_keyword:
            payload["keyword"] = confirmed_keyword
        if domain:
            payload["domain"] = domain
        if custom_title:
            payload["custom_title"] = custom_title
        if delivery_mode is not None:
            payload["delivery_mode"] = delivery_mode
            payload["delivery_mode_confirmed"] = bool(delivery_mode_confirmed)
            
        response = await self._request(
            "POST",
            f"/api/v1/content/jobs/{job_id}/confirm",
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_content_org_config(self, slack_user_id: str, domain: Optional[str] = None) -> Optional[dict]:
        """
        Check if content factory org config exists for a user.
        
        Args:
            slack_user_id: The Slack ID of the user
            
        Returns:
            Config dict or None if not found
        """
        if not self.base_url:
            return None
        
        # Clean ID here just in case caller didn't
        clean_id = self._clean_slack_id(slack_user_id)
        params = {"slack_user_id": clean_id}
        if domain:
            params["domain"] = domain
        
        response = await self._request(
            "GET",
            "/api/content-factory/org/config",
            params=params,
            timeout=5.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def set_article_delivery_mode(
        self,
        job_id: str,
        delivery_mode: str,
        *,
        request_source: str = CONTENT_FACTORY_REQUEST_SOURCE,
    ) -> dict:
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        response = await self._request(
            "POST",
            f"/api/v1/content/jobs/{job_id}/delivery-mode",
            json={
                "delivery_mode": delivery_mode,
                "request_source": request_source,
            },
            timeout=30.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def discover_opportunities(
        self,
        domain: str,
        competitors: list[str],
        seed_keywords: Optional[list[str]] = None
    ) -> list[dict]:
        """
        Discover content opportunities via mlai-backend.
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
            
        payload = {
            "domain": domain,
            "competitors": competitors,
            "seed_keywords": seed_keywords
        }
        
        try:
            response = await self._request(
                "POST",
                "/api/v1/content/discover",
                json=payload,
                timeout=60.0,
                circuit_breaker=True,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("opportunities", [])
        except Exception as e:
            print(f"Discovery failed: {e}")
            raise

    async def check_generation_status(self, job_id: str) -> dict:
        """Check status of a generation job."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        response = await self._request(
            "GET",
            f"/api/v1/content/jobs/{job_id}",
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def cancel_job(self, job_id: str, slack_user_id: str) -> dict:
        """
        Cancel a content generation job.

        Args:
            job_id: The job ID to cancel
            slack_user_id: The Slack user requesting cancellation

        Returns:
            Response from backend
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        payload = {
            "action": "cancel",
            "slack_user_id": self._clean_slack_id(slack_user_id)
        }

        response = await self._request(
            "POST",
            f"/api/v1/content/jobs/{job_id}/cancel",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
        )
        if response.status_code == 404:
            return {"status": "not_found", "message": "Job not found or already completed"}
        response.raise_for_status()
        return response.json()

    # =========================================================================
    # Points / Member Endpoints (Merged from mlai_points/client.py)
    # =========================================================================
    
    async def get_balance(self, slack_user_id: str) -> dict:
        """Get points balance for a user."""
        if not self.base_url:
            return {}
        response = await self._request(
            "GET",
            f"{self._points_base}/users/{slack_user_id}/balance/",
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def list_committee_candidate_emails(
        self,
        requester_slack_id: str,
    ) -> dict:
        """Return eligible member emails for an authorised committee admin."""
        response = await self._request(
            "POST",
            f"{self._points_base}/committee-candidates/emails/",
            json={
                "requester_slack_id": self._clean_slack_id(requester_slack_id),
            },
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()
    
    async def get_history(self, slack_user_id: str, limit: int = 10) -> List[dict]:
        """Get recent ledger entries for a user."""
        if not self.base_url:
            return []
        response = await self._request(
            "GET",
            f"{self._points_base}/ledger/",
            params={"slack_user_id": slack_user_id},
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()[:limit]
    
    async def list_tasks(
        self,
        status: Optional[str] = "open",
        portfolio: Optional[str] = None,
        *,
        claimable: Optional[bool] = None,
        assigned_to_me: Optional[str] = None,
        reviewer_slack_id: Optional[str] = None,
        needs_review: Optional[bool] = None,
        volunteer_ready: Optional[bool] = None,
        work_domain: Optional[str] = None,
        review_flow: Optional[str] = None,
        task_code: Optional[str] = None,
    ) -> List[dict]:
        """List tasks, optionally filtered by status and portfolio."""
        if not self.base_url:
            return []
        params = {}
        if status:
            params["status"] = status
        if portfolio:
            params["portfolio"] = portfolio
        if claimable is not None:
            params["claimable"] = str(bool(claimable)).lower()
        if assigned_to_me:
            params["assigned_to_me"] = self._clean_slack_id(assigned_to_me)
        if reviewer_slack_id:
            params["reviewer_slack_id"] = self._clean_slack_id(reviewer_slack_id)
        if needs_review is not None:
            params["needs_review"] = str(bool(needs_review)).lower()
        if volunteer_ready is not None:
            params["volunteer_ready"] = str(bool(volunteer_ready)).lower()
        if work_domain:
            params["work_domain"] = work_domain
        if review_flow:
            params["review_flow"] = review_flow
        if task_code:
            params["task_code"] = task_code
        response = await self._request(
            "GET",
            f"{self._points_base}/tasks/",
            params=params,
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_task_by_code(self, task_code: str) -> dict:
        """Get a specific task by volunteer-facing task code."""
        response = await self._request(
            "GET",
            f"{self._points_base}/tasks/by-code/{task_code}/",
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def _resolve_task_id(self, task_identifier: Union[int, str]) -> int:
        """Resolve numeric task ids and ROO task codes to a numeric id."""
        if isinstance(task_identifier, int):
            return task_identifier

        identifier = str(task_identifier).strip()
        if identifier.startswith("#"):
            identifier = identifier[1:]
        if identifier.isdigit():
            return int(identifier)
        if identifier.upper().startswith("ROO-"):
            task = await self.get_task_by_code(identifier.upper())
            if not task.get("id"):
                raise ValueError(f"Task {identifier} did not return an id")
            return int(task["id"])
        raise ValueError(f"Unsupported task identifier: {task_identifier}")

    async def claim_task(self, task_id: Union[int, str], slack_user_id: str) -> dict:
        """Claim a task for completion."""
        resolved_task_id = await self._resolve_task_id(task_id)
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/{resolved_task_id}/claim/",
            json={"slack_user_id": self._clean_slack_id(slack_user_id)},
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def unclaim_task(self, task_id: Union[int, str], slack_user_id: str) -> dict:
        """Release a claimed task before any submission exists."""
        resolved_task_id = await self._resolve_task_id(task_id)
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/{resolved_task_id}/unclaim/",
            json={"slack_user_id": self._clean_slack_id(slack_user_id)},
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def submit_task(self, task_id: Union[int, str], slack_user_id: str, submission_text: str, submission_url: Optional[str] = None) -> dict:
        """Submit completed work for a task."""
        resolved_task_id = await self._resolve_task_id(task_id)
        payload = {
            "slack_user_id": slack_user_id,
            "submission_text": submission_text,
        }
        if submission_url:
            payload["submission_url"] = submission_url
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/{resolved_task_id}/submit/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def check_coworking(
        self,
        check_date: Optional[str] = None,
        days: int = 7,
        slack_user_id: Optional[str] = None,
    ) -> List[dict]:
        """Check coworking availability.

        When ``slack_user_id`` is supplied the backend quotes the price that
        user would actually be charged (which drops with a current monthly
        update), so the displayed cost matches the booking cost.
        """
        params = {"days": days}
        if check_date:
            params["date"] = check_date
        if slack_user_id:
            params["slack_user_id"] = self._clean_slack_id(slack_user_id)
        response = await self._request(
            "GET",
            f"{self._points_base}/coworking/availability/",
            params=params,
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_coworking_report(self, slack_user_id: str, start_date: str, end_date: str) -> dict:
        """Get active coworking booking report for an inclusive date range."""
        response = await self._request(
            "GET",
            f"{self._points_base}/coworking/report/",
            params={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "start_date": start_date,
                "end_date": end_date,
            },
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def list_meeting_rooms(self) -> list[dict]:
        """Return active meeting rooms without member or booking identity."""
        response = await self._request(
            "GET",
            f"{self._points_base}/meeting-rooms/rooms/",
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        payload = response.json()
        rooms = payload.get("rooms") if isinstance(payload, dict) else None
        return rooms if isinstance(rooms, list) else []

    async def check_meeting_room_availability(
        self,
        slack_user_id: str,
        *,
        room_slug: str,
        date: Optional[str] = None,
        starts_at: Optional[str] = None,
        ends_at: Optional[str] = None,
        target_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Check one room date or exact interval without exposing booker identity."""
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "room_slug": room_slug,
        }
        if date:
            payload["date"] = date
        if starts_at:
            payload["starts_at"] = starts_at
        if ends_at:
            payload["ends_at"] = ends_at
        if target_slack_user_id:
            payload["target_slack_user_id"] = self._clean_slack_id(
                target_slack_user_id
            )
        response = await self._request(
            "POST",
            f"{self._points_base}/meeting-rooms/availability/",
            json=payload,
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def book_meeting_room(
        self,
        slack_user_id: str,
        *,
        room_slug: str,
        starts_at: str,
        ends_at: str,
        client_request_id: str,
        confirmation_expires_at: str,
        expected_points_cost: int,
        slack_channel_id: Optional[str] = None,
        target_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Confirm one self or authorized Points Admin meeting-room booking."""
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "room_slug": room_slug,
            "starts_at": starts_at,
            "ends_at": ends_at,
            "client_request_id": client_request_id,
            "confirmation_expires_at": confirmation_expires_at,
            "expected_points_cost": expected_points_cost,
        }
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if target_slack_user_id:
            payload["target_slack_user_id"] = self._clean_slack_id(
                target_slack_user_id
            )
        response = await self._request(
            "POST",
            f"{self._points_base}/meeting-rooms/book/",
            json=payload,
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.5,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_my_meeting_room_bookings(self, slack_user_id: str) -> List[dict]:
        """Return the requesting member's upcoming active room bookings."""
        response = await self._request(
            "POST",
            f"{self._points_base}/meeting-rooms/my-bookings/",
            json={"slack_user_id": self._clean_slack_id(slack_user_id)},
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json().get("bookings", [])

    async def cancel_meeting_room_booking(
        self,
        slack_user_id: str,
        booking_id: str,
    ) -> dict:
        """Cancel and refund one booking owned by the requesting member."""
        response = await self._request(
            "POST",
            f"{self._points_base}/meeting-rooms/cancel/",
            json={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "booking_id": booking_id,
            },
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.5,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_luma_attendee_report(
        self,
        slack_user_id: str,
        *,
        event_count: int = 3,
        event_date: Optional[str] = None,
        approval_status: str = "approved",
        include_csv: bool = False,
    ) -> dict:
        """Get Luma attendee summaries and optional CSV payloads from mlai-backend."""
        params = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "event_count": event_count,
            "approval_status": approval_status,
            "include_csv": "true" if include_csv else "false",
        }
        if event_date:
            params["event_date"] = event_date

        response = await self._request(
            "GET",
            "/api/v1/integrations/luma/attendee-report",
            params=params,
            timeout=30.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_reconciliation_report(
        self,
        slack_user_id: str,
        *,
        days: int = 30,
        since: Optional[str] = None,
        until: Optional[str] = None,
        include_workbook: bool = True,
    ) -> dict:
        """Get the Luma→Stripe reconciliation report (Points Admin only) from mlai-backend.

        Returns per-payout JSON plus a base64 markdown brief and optional xlsx
        workbook. Read-only; mlai-backend enforces the Points-Admin role.
        """
        params = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "days": days,
            "include_workbook": "true" if include_workbook else "false",
        }
        if since:
            params["since"] = since
        if until:
            params["until"] = until

        response = await self._request(
            "GET",
            "/api/v1/integrations/reconciliation/report",
            params=params,
            timeout=60.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_event_finance_audit(
        self,
        slack_user_id: str,
        *,
        since: str,
        until: str,
        domain: str = "mlai.au",
    ) -> dict:
        """Get the read-only event revenue/cost completeness audit."""
        response = await self._request(
            "GET",
            "/api/v1/integrations/reconciliation/event-finance-audit",
            params={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "domain": str(domain or "mlai.au").strip().lower(),
                "since": since,
                "until": until,
            },
            timeout=90.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_statement_reconciliation_readiness(
        self,
        slack_user_id: str,
        *,
        domain: str = "mlai.au",
    ) -> dict:
        """Check queue freshness, monthly context and Xero write configuration."""
        response = await self._request(
            "GET",
            "/api/v1/integrations/reconciliation/readiness",
            params={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "domain": str(domain or "mlai.au").strip().lower(),
            },
            timeout=30.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def start_statement_reconciliation_run(
        self,
        slack_user_id: str,
        *,
        domain: str = "mlai.au",
        instruction: Optional[str] = None,
        statement_line_ids: Optional[List[str]] = None,
    ) -> dict:
        """Start a preview-only Xero statement reconciliation agent run."""
        payload: Dict[str, Any] = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "domain": str(domain or "mlai.au").strip().lower(),
        }
        if instruction:
            payload["instruction"] = str(instruction).strip()
        if statement_line_ids:
            payload["statement_line_ids"] = [
                str(item).strip() for item in statement_line_ids if str(item).strip()
            ]
        response = await self._request(
            "POST",
            "/api/v1/integrations/reconciliation/agent-runs",
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_statement_reconciliation_outcomes(
        self,
        slack_user_id: str,
        *,
        domain: str = "mlai.au",
        limit: int = 50,
    ) -> dict:
        """Get confirmed human outcomes and read-only learning candidates."""
        response = await self._request(
            "GET",
            "/api/v1/integrations/reconciliation/outcomes",
            params={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "domain": str(domain or "mlai.au").strip().lower(),
                "limit": max(1, min(int(limit), 200)),
            },
            timeout=30.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def decide_statement_reconciliation_learning_candidate(
        self,
        slack_user_id: str,
        candidate_id: str,
        *,
        candidate_version: str,
        decision: str,
        reason: Optional[str] = None,
        domain: str = "mlai.au",
    ) -> dict:
        """Explicitly promote or reject one previously reviewed learning candidate."""
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "domain": str(domain or "mlai.au").strip().lower(),
            "candidate_version": str(candidate_version or "").strip(),
            "decision": str(decision or "").strip().lower(),
            "confirm": True,
        }
        if reason:
            payload["reason"] = str(reason).strip()
        response = await self._request(
            "POST",
            (
                "/api/v1/integrations/reconciliation/learning-candidates/"
                f"{quote(str(candidate_id).strip(), safe='')}"
            ),
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_statement_reconciliation_run(
        self,
        slack_user_id: str,
        run_id: str,
        *,
        domain: str = "mlai.au",
    ) -> dict:
        """Get the current state and suggestions for one reconciliation run."""
        response = await self._request(
            "GET",
            f"/api/v1/integrations/reconciliation/agent-runs/{quote(str(run_id).strip(), safe='')}",
            params={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "domain": str(domain or "mlai.au").strip().lower(),
            },
            timeout=30.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def retry_statement_reconciliation_run(
        self,
        slack_user_id: str,
        run_id: str,
        *,
        domain: str = "mlai.au",
    ) -> dict:
        """Retry only the context-analysis dispatch for an existing durable run."""
        response = await self._request(
            "POST",
            (
                "/api/v1/integrations/reconciliation/agent-runs/"
                f"{quote(str(run_id).strip(), safe='')}/retry"
            ),
            json={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "domain": str(domain or "mlai.au").strip().lower(),
                "confirm": True,
            },
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def preview_statement_reconciliation_run(
        self,
        slack_user_id: str,
        run_id: str,
        *,
        domain: str = "mlai.au",
    ) -> dict:
        """Build current write previews without recording approval or writing Xero."""
        response = await self._request(
            "GET",
            f"/api/v1/integrations/reconciliation/agent-runs/{quote(str(run_id).strip(), safe='')}/preview",
            params={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "domain": str(domain or "mlai.au").strip().lower(),
            },
            timeout=30.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def approve_ready_statement_reconciliation_run(
        self,
        slack_user_id: str,
        run_id: str,
        *,
        domain: str = "mlai.au",
        decision_request_id: Optional[str] = None,
    ) -> dict:
        """Record an explicit admin approval for every currently ready preview."""
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "domain": str(domain or "mlai.au").strip().lower(),
            "confirm": True,
            "approve_all_ready": True,
            "decision_request_id": decision_request_id or f"roo-{uuid4().hex}",
        }
        response = await self._request(
            "POST",
            f"/api/v1/integrations/reconciliation/agent-runs/{quote(str(run_id).strip(), safe='')}/decisions",
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def reject_statement_reconciliation_suggestions(
        self,
        slack_user_id: str,
        run_id: str,
        suggestion_ids: List[int],
        *,
        reason: str,
        domain: str = "mlai.au",
        decision_request_id: Optional[str] = None,
    ) -> dict:
        """Record an explicit admin rejection for selected run suggestions."""
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "domain": str(domain or "mlai.au").strip().lower(),
            "confirm": True,
            "decision_request_id": decision_request_id or f"roo-{uuid4().hex}",
            "decisions": [
                {"suggestion_id": int(item), "decision": "reject", "reason": str(reason).strip()}
                for item in suggestion_ids
            ],
        }
        response = await self._request(
            "POST",
            f"/api/v1/integrations/reconciliation/agent-runs/{quote(str(run_id).strip(), safe='')}/decisions",
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def execute_approved_statement_reconciliation_run(
        self,
        slack_user_id: str,
        run_id: str,
        *,
        domain: str = "mlai.au",
        suggestion_ids: Optional[List[int]] = None,
    ) -> dict:
        """Write only explicitly approved, unchanged suggestions to Xero."""
        payload: Dict[str, Any] = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "domain": str(domain or "mlai.au").strip().lower(),
            "confirm": True,
        }
        if suggestion_ids:
            payload["suggestion_ids"] = [int(item) for item in suggestion_ids]
        response = await self._request(
            "POST",
            f"/api/v1/integrations/reconciliation/agent-runs/{quote(str(run_id).strip(), safe='')}/execute",
            json=payload,
            timeout=60.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()


    async def get_data_catalog(self, requester_slack_id: str) -> dict:
        """Get the requester's curated read-only data resource catalog."""
        response = await self._request(
            "GET",
            f"{self._data_base}/catalog/",
            params={
                "requester_slack_id": self._clean_slack_id(requester_slack_id),
            },
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def query_data(self, payload: dict) -> dict:
        """Query a curated read-only data resource through mlai-backend."""
        response = await self._request(
            "POST",
            f"{self._data_base}/query/",
            json=payload,
            timeout=35.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def list_linear_channel_issues(
        self,
        *,
        slack_workspace_id: str,
        slack_channel_id: str,
        requester_slack_id: str,
        limit: int = 50,
        after: Optional[str] = None,
    ) -> dict:
        """List the live Linear queue bound to the invoking Slack channel."""
        payload: Dict[str, Any] = {
            "slack_workspace_id": str(slack_workspace_id or "").strip(),
            "slack_channel_id": str(slack_channel_id or "").strip(),
            "requester_slack_id": self._clean_slack_id(requester_slack_id),
            "limit": max(1, min(int(limit or 50), 100)),
        }
        if after:
            payload["after"] = str(after).strip()
        response = await self._request(
            "POST",
            "/api/v1/integrations/linear/channel-issues/list",
            json=payload,
            timeout=30.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def get_linear_channel_issue(
        self,
        *,
        slack_workspace_id: str,
        slack_channel_id: str,
        requester_slack_id: str,
        issue_identifier: str,
        include_comments: bool = True,
    ) -> dict:
        """Get one approved Linear issue and its bounded comment history."""
        response = await self._request(
            "POST",
            "/api/v1/integrations/linear/channel-issues/detail",
            json={
                "slack_workspace_id": str(slack_workspace_id or "").strip(),
                "slack_channel_id": str(slack_channel_id or "").strip(),
                "requester_slack_id": self._clean_slack_id(requester_slack_id),
                "issue_identifier": str(issue_identifier or "").strip(),
                "include_comments": bool(include_comments),
            },
            timeout=45.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def book_coworking(self, slack_user_id: str, booking_date: str, slack_channel_id: Optional[str] = None) -> dict:
        """Book a coworking day."""
        try:
            from datetime import datetime
            current_time = datetime.now().isoformat()
        except Exception:
            current_time = ""
        payload = {
            "slack_user_id": slack_user_id,
            "date": booking_date,
            "current_time": current_time,
        }
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        response = await self._request(
            "POST",
            f"{self._points_base}/coworking/book/",
            json=payload,
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.5,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def book_coworking_many(
        self,
        admin_slack_user_id: str,
        target_slack_user_ids: List[str],
        booking_date: str,
        slack_channel_id: Optional[str] = None,
    ) -> dict:
        """Book multiple coworking days as a full Points Admin."""
        try:
            from datetime import datetime
            current_time = datetime.now().isoformat()
        except Exception:
            current_time = ""

        cleaned_targets: List[str] = []
        for target in target_slack_user_ids or []:
            cleaned = self._clean_slack_id(target)
            if cleaned and cleaned not in cleaned_targets:
                cleaned_targets.append(cleaned)

        payload = {
            "admin_slack_user_id": self._clean_slack_id(admin_slack_user_id),
            "target_slack_user_ids": cleaned_targets,
            "date": booking_date,
            "current_time": current_time,
        }
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id

        response = await self._request(
            "POST",
            f"{self._points_base}/coworking/book-many/",
            json=payload,
            timeout=20.0,
            transport_retries=1,
            retry_backoff_seconds=0.5,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def claim_office_manager_day(
        self,
        slack_user_id: str,
        booking_date: str,
        attempt_id: str = "",
    ) -> dict:
        """Claim today's Office Manager role using the verified Slack actor."""
        if not self.roo_api_key:
            raise BackendIdentityError(
                "ROO_API_KEY is required for Office Manager claims"
            )
        attempt_id = str(attempt_id or "").strip()
        try:
            parsed_attempt_id = UUID(attempt_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("attempt_id must be a canonical UUID") from exc
        if str(parsed_attempt_id) != attempt_id:
            raise ValueError("attempt_id must be a canonical UUID")
        response = await self._request(
            "POST",
            f"{self._points_base}/coworking/office-manager/claim/",
            json={
                "slack_user_id": self._clean_slack_id(slack_user_id),
                "date": str(booking_date or "").strip(),
                "attempt_id": attempt_id,
            },
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.5,
            circuit_breaker=True,
            redact_logs=True,
        )
        response.raise_for_status()
        return response.json()
    
    async def get_github_auth_url(self, slack_user_id: str, domain: Optional[str] = None) -> dict:
        """Get the GitHub OAuth URL for a user from the backend."""
        params = {"slack_user_id": slack_user_id}
        if domain:
            params["domain"] = domain
        response = await self._request(
            "GET",
            "/api/v1/integrations/github/auth-url",
            params=params,
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_backend_readiness(self) -> dict:
        response = await self._request(
            "GET",
            "/healthz/ready",
            timeout=5.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=False,
        )
        response.raise_for_status()
        return response.json()

    async def get_points_health(self) -> dict:
        response = await self._request(
            "GET",
            "/healthz/points",
            timeout=5.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=False,
        )
        response.raise_for_status()
        return response.json()

    async def reconnect_content_factory_github(
        self,
        slack_user_id: str,
        domain: Optional[str] = None,
        github_repo: Optional[str] = None,
        trigger: str = "manual",
        pending_action: Optional[str] = None,
    ) -> dict:
        """Start or confirm the Content Factory GitHub reconnect flow."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "trigger": trigger,
        }
        if domain:
            payload["domain"] = domain
        if github_repo:
            payload["github_repo"] = github_repo
        if pending_action:
            payload["pending_action"] = pending_action

        response = await self._request(
            "POST",
            "/api/content-factory/github/reconnect",
            json=payload,
            timeout=20.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_integration(
        self,
        slack_user_id: str,
        domain: Optional[str] = None,
        *,
        include_repo_freshness: bool = False,
    ) -> Optional[dict]:
        """Check if user has a valid GitHub integration.

        Args:
            slack_user_id: Slack user ID
            domain: Optional domain to check domain-specific GitHub connection.
                    When provided, response includes domain_connected, domain_github_repo,
                    needs_github_auth, and oauth_url fields.
            include_repo_freshness: When True, ask mlai-backend to perform live GitHub freshness checks.
        """
        clean_id = self._clean_slack_id(slack_user_id)
        params = {}
        if domain:
            params["domain"] = domain
        params["include_repo_freshness"] = "1" if include_repo_freshness else "0"

        response = await self._request(
            "GET",
            f"/api/v1/integrations/github/{clean_id}/",
            params=params,
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def save_pending_intent(self, slack_user_id: str, intent: Any) -> None:
        """Save a pending intent to resume after auth."""
        response = await self._request(
            "POST",
            "/api/v1/integrations/pending-intent/",
            json={"slack_user_id": slack_user_id, "intent": intent},
            timeout=15.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()

    async def trigger_repo_scan(
        self,
        slack_user_id: str,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
        domain: Optional[str] = None,
        request_source: str = CONTENT_FACTORY_REQUEST_SOURCE,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """
        Trigger a repository scan for a user via the backend.

        Including the domain ensures the Content Factory scan can scrape and
        persist company context for auto-write mode.
        """
        try:
            clean_id = self._clean_slack_id(slack_user_id)
            payload = {
                "slack_user_id": clean_id,
                "request_source": request_source,
            }
            if requested_by_slack_user_id:
                payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)
            if domain:
                payload["domain"] = domain
            if slack_channel_id:
                payload["slack_channel_id"] = slack_channel_id
            if slack_thread_ts:
                payload["slack_thread_ts"] = slack_thread_ts
            
            response = await self._request(
                "POST",
                "/api/v1/integrations/github/scan",
                json=payload,
                timeout=60.0,
                circuit_breaker=True,
            )
            if response.status_code == 202:
                data = response.json()
                return {
                    "status": data.get("status", "accepted"),
                    "message": data.get("message", "Scan queued successfully."),
                    **data,
                }
            if response.status_code == 404:
                return {"error": "no_integration", "message": "No GitHub integration found for this user."}
            if response.status_code == 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {"error": response.text}
                if error_data.get("available_domains"):
                    return {
                        "error": "multiple_domains",
                        "available_domains": error_data["available_domains"],
                        "message": error_data.get("error", "Multiple domains available"),
                        "hint": error_data.get("hint", "")
                    }
                if error_data.get("needs_github_auth"):
                    return {
                        "error": "needs_github_auth",
                        "needs_github_auth": True,
                        "oauth_url": error_data.get("oauth_url"),
                        "domain": error_data.get("domain"),
                        "message": error_data.get("error", "GitHub not connected for this domain")
                    }
                return {"error": "bad_request", "message": error_data.get("error", "Bad request")}
            response.raise_for_status()
            return response.json()
        except MLAIBackendUnavailableError:
            raise
        except Exception as e:
            print(f"Failed to trigger repo scan: {e}")
            return {"error": "scan_failed", "message": str(e)}

    async def scaffold_articles(
        self,
        domain: str,
        slack_user_id: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """
        Trigger articles directory scaffolding for a domain.

        Args:
            domain: The domain to scaffold articles for
            slack_user_id: The Slack user requesting the scaffold
            slack_channel_id: The Slack channel for thread replies
            slack_thread_ts: The Slack thread timestamp for replies

        Returns:
            Response dict with status_code and data
        """
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        clean_id = self._clean_slack_id(slack_user_id)
        payload = {
            "domain": domain,
            "slack_user_id": clean_id,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts
        }
        if requested_by_slack_user_id:
            payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)

        response = await self._request(
            "POST",
            "/api/v1/integrations/github/scaffold",
            json=payload,
            timeout=60.0,
            circuit_breaker=True,
        )
        return {
            "status_code": response.status_code,
            "data": response.json() if response.status_code < 500 else {}
        }

    async def decide_scaffold(
        self,
        *,
        scan_run_id: str,
        decision: str,
        domain: str,
        slack_user_id: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Approve or deny scaffold creation for a scan run."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        clean_id = self._clean_slack_id(slack_user_id)
        payload = {
            "scan_run_id": scan_run_id,
            "decision": decision,
            "domain": domain,
            "slack_user_id": clean_id,
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
        }
        if requested_by_slack_user_id:
            payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)

        response = await self._request(
            "POST",
            "/api/v1/integrations/github/scaffold/decision",
            json=payload,
            timeout=60.0,
            circuit_breaker=True,
        )
        return {
            "status_code": response.status_code,
            "data": response.json() if response.status_code < 500 else {},
        }

    async def decide_article_system(
        self,
        *,
        domain: str,
        slack_user_id: str,
        decision: str,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Persist an article-system decision and optionally resume the pending write."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        clean_id = self._clean_slack_id(slack_user_id)
        payload = {
            "domain": domain,
            "slack_user_id": clean_id,
            "decision": decision,
        }
        if requested_by_slack_user_id:
            payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)

        response = await self._request(
            "POST",
            "/api/v1/content/article-system/decision",
            json=payload,
            timeout=60.0,
            circuit_breaker=True,
        )
        return {
            "status_code": response.status_code,
            "data": response.json() if response.status_code < 500 else {},
        }

    async def publish_article(self, job_id: str, slack_user_id: str) -> dict:
        """Request publication of a completed article."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")
        
        # Ensure we have a clean ID
        clean_id = self._clean_slack_id(slack_user_id)
            
        response = await self._request(
            "POST",
            f"/api/v1/content/publish/{job_id}",
            json={"slack_user_id": clean_id},
            timeout=60.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def publish_article_as_pr(
        self,
        job_id: str,
        slack_user_id: str,
        *,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Promote a completed content-only article into a draft-PR publish run."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        clean_id = self._clean_slack_id(slack_user_id)
        payload = {"slack_user_id": clean_id}
        if requested_by_slack_user_id:
            payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)

        response = await self._request(
            "POST",
            f"/api/v1/content/jobs/{job_id}/publish-pr",
            json=payload,
            timeout=60.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def resolve_content_thread(
        self,
        *,
        slack_user_id: str,
        slack_channel_id: str,
        slack_thread_ts: str,
        requested_action: str,
        domain: Optional[str] = None,
        job_id: Optional[str] = None,
        requested_by_slack_user_id: Optional[str] = None,
    ) -> dict:
        """Resolve the active content-factory job for a Slack thread."""
        if not self.base_url:
            raise ValueError("MLAI_BACKEND_URL not configured")

        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "slack_channel_id": slack_channel_id,
            "slack_thread_ts": slack_thread_ts,
            "requested_action": requested_action,
        }
        if requested_by_slack_user_id:
            payload["requested_by_slack_user_id"] = self._clean_slack_id(requested_by_slack_user_id)
        if domain:
            payload["domain"] = domain
        if job_id:
            payload["job_id"] = job_id

        response = await self._request(
            "POST",
            "/api/v1/content/jobs/resolve-thread",
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    # =========================================================================
    # Missing Admin / Points Methods
    # =========================================================================

    async def get_task(self, task_id: Union[int, str]) -> dict:
        """Get a specific task by ID."""
        resolved_task_id = await self._resolve_task_id(task_id)
        response = await self._request(
            "GET",
            f"{self._points_base}/tasks/{resolved_task_id}/",
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_my_bookings(self, slack_user_id: str) -> List[dict]:
        """Get user's coworking bookings."""
        response = await self._request(
            "GET",
            f"{self._points_base}/coworking/my-bookings/",
            params={"slack_user_id": slack_user_id},
            timeout=10.0,
            transport_retries=1,
            retry_backoff_seconds=0.25,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()
            
    async def cancel_coworking(
        self,
        slack_user_id: str,
        booking_id: Optional[str] = None,
        booking_date: Optional[str] = None
    ) -> dict:
        """Cancel a coworking booking."""
        payload = {"slack_user_id": slack_user_id}
        if booking_id:
            payload["booking_id"] = booking_id
        elif booking_date:
            payload["date"] = booking_date
            
        response = await self._request(
            "POST",
            f"{self._points_base}/coworking/cancel/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def get_rate_card(self) -> List[dict]:
        """Get the automated rate card for point awards."""
        try:
            response = await self._request(
                "GET",
                f"{self._points_base}/rate-card/",
                timeout=5.0,
                transport_retries=1,
                retry_backoff_seconds=0.25,
                circuit_breaker=True,
            )
            if response.status_code == 404:
                return []
            response.raise_for_status()
            return response.json()
        except MLAIBackendUnavailableError:
            raise
        except Exception as e:
            print(f"❌ Failed to fetch rate card: {e}")
            return []

    async def is_admin(self, slack_user_id: str) -> bool:
        """Check if a user is a full Points Admin (with caching)."""
        if slack_user_id in self._admin_cache:
            return self._admin_cache[slack_user_id]
        
        try:
            details = await self.get_admin_details(slack_user_id)
            is_admin = (
                isinstance(details, dict)
                and str(details.get("role") or "").strip().lower() in FULL_POINTS_ADMIN_ROLES
            )
            self._admin_cache[slack_user_id] = is_admin
            return is_admin
        except Exception:
            return False

    async def get_admin_details(self, slack_user_id: str) -> Optional[dict]:
        """Get details for a Points Admin."""
        try:
            response = await self._request(
                "GET",
                f"{self._points_base}/admins/{slack_user_id}/",
                timeout=10.0,
                transport_retries=1,
                retry_backoff_seconds=0.25,
                circuit_breaker=True,
            )
            if response.status_code == 200:
                return response.json()
            return None
        except MLAIBackendUnavailableError:
            raise
        except Exception as e:
            print(f"Failed to fetch admin details: {e}")
            return None

    async def get_admin_allowance(self, slack_user_id: str) -> dict:
        """Get the admin's weekly allowance status."""
        try:
            response = await self._request(
                "GET",
                f"{self._points_base}/admin/allowance/",
                params={"slack_id": slack_user_id},
                timeout=10.0,
                transport_retries=1,
                retry_backoff_seconds=0.25,
                circuit_breaker=True,
                use_admin_headers=True,
            )
            if response.status_code == 404:
                return {'error': 'Not a points admin'}
            response.raise_for_status()
            return response.json()
        except MLAIBackendUnavailableError:
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return {'error': 'Not a points admin'}
            raise
        except Exception as e:
            print(f"❌ Failed to fetch admin allowance: {e}")
            return {'error': str(e)}

    async def promote_points_admin(
        self,
        requester_slack_id: str,
        target_slack_id: str,
    ) -> dict:
        """Promote a user to Points Admin using the privileged admin API."""
        cleaned_target = self._clean_slack_id(target_slack_id)
        payload = {
            "requester_slack_id": requester_slack_id,
            "target_slack_id": cleaned_target,
        }

        response = await self._request(
            "POST",
            f"{self._points_base}/admins/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        if response.status_code == 409:
            return response.json()

        response.raise_for_status()
        result = response.json()
        self._admin_cache[cleaned_target] = True
        return result

    async def set_points_admin_weekly_allowance(
        self,
        requester_slack_id: str,
        target_slack_id: str,
        weekly_allowance: int,
    ) -> dict:
        """Update a specific Points Admin's weekly allowance."""
        cleaned_target = self._clean_slack_id(target_slack_id)
        payload = {
            "requester_slack_id": requester_slack_id,
            "weekly_allowance": weekly_allowance,
        }

        response = await self._request(
            "PATCH",
            f"{self._points_base}/admins/{cleaned_target}/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        if response.status_code == 404:
            return response.json()

        response.raise_for_status()
        return response.json()

    async def revoke_points_admin(
        self,
        requester_slack_id: str,
        target_slack_id: str,
    ) -> dict:
        """Revoke a user's Points Admin access using the privileged admin API."""
        cleaned_target = self._clean_slack_id(target_slack_id)
        payload = {"requester_slack_id": requester_slack_id}

        response = await self._request(
            "DELETE",
            f"{self._points_base}/admins/{cleaned_target}/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        if response.status_code == 404:
            return response.json()

        response.raise_for_status()
        result = response.json()
        self._admin_cache[cleaned_target] = False
        return result

    async def create_task(
        self,
        admin_slack_id: str,
        title: str,
        points: int,
        description: str = "",
        portfolio: str = "events",
        due_date: Optional[str] = None,
        assigned_to_user_id: Optional[str] = None,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None
    ) -> dict:
        """Create a new task (admin only)."""
        payload = {
            "title": title,
            "points": points,
            "description": description,
            "portfolio": portfolio,
            "created_by_user_id": admin_slack_id,
            "status": "open",
        }
        if due_date:
            payload["due_date"] = due_date
        if assigned_to_user_id:
            payload["assigned_to_user_id"] = self._clean_slack_id(assigned_to_user_id)
            payload["status"] = "claimed"
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts
            
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        if response.status_code == 403:
            return {"error": "forbidden", "message": response.json().get("error")}

        response.raise_for_status()
        return response.json()

    async def approve_task(
        self,
        task_id: Union[int, str],
        admin_slack_id: str,
        submission_id: Optional[str] = None
    ) -> dict:
        """Approve a task submission (admin only)."""
        resolved_task_id = await self._resolve_task_id(task_id)
        payload = {"slack_user_id": admin_slack_id}
        if submission_id:
            payload["submission_id"] = submission_id
            
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/{resolved_task_id}/approve/",
            json=payload,
            timeout=15.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def reject_task(
        self,
        task_id: Union[int, str],
        admin_slack_id: str,
        reason: str = "",
        submission_id: Optional[str] = None
    ) -> dict:
        """Reject a task submission (admin only)."""
        resolved_task_id = await self._resolve_task_id(task_id)
        payload = {
            "slack_user_id": admin_slack_id,
            "reason": reason,
        }
        if submission_id:
            payload["submission_id"] = submission_id
            
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/{resolved_task_id}/reject/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def update_task(
        self,
        task_id: Union[int, str],
        admin_slack_id: str,
        updates: dict,
        *,
        expected_updated_at: str,
    ) -> dict:
        """Edit a task through the admin task update contract."""
        resolved_task_id = await self._resolve_task_id(task_id)
        payload = {
            **updates,
            "slack_user_id": self._clean_slack_id(admin_slack_id),
            "expected_updated_at": expected_updated_at,
        }
        response = await self._request(
            "PATCH",
            f"{self._points_base}/tasks/{resolved_task_id}/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def cancel_task(
        self,
        task_id: Union[int, str],
        admin_slack_id: str,
        reason: Optional[str] = None,
    ) -> dict:
        """Cancel/archive a task (admin only)."""
        resolved_task_id = await self._resolve_task_id(task_id)
        payload = {"slack_user_id": self._clean_slack_id(admin_slack_id)}
        if reason:
            payload["reason"] = reason
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/{resolved_task_id}/cancel/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def award_task(self, task_id: int, admin_slack_id: str, target_slack_id: str) -> dict:
        """Direct award a task (claim + approve) to a user (admin only)."""
        payload = {
            "created_by_user_id": admin_slack_id,
            "assigned_to_user_id": self._clean_slack_id(target_slack_id),
        }
        response = await self._request(
            "POST",
            f"{self._points_base}/tasks/{task_id}/award/",
            json=payload,
            timeout=15.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def award_points(
        self,
        admin_slack_id: str,
        target_slack_id: str,
        points: int,
        reason: str
    ) -> dict:
        """Manually award or deduct points (admin only)."""
        # 1. Pre-flight Admin Check
        is_admin = await self.is_admin(admin_slack_id)
        if not is_admin:
            raise PermissionError(f"User {admin_slack_id} is not a Points Admin.")

        # 2. Pre-flight Self-Award Check
        cleaned_target = self._clean_slack_id(target_slack_id)
        if admin_slack_id == cleaned_target and points > 0:
            raise ValueError("Nice try! You can't award points to yourself. 😉")

        # 3. Pre-flight Negative Check
        if points < 0:
            raise ValueError("Point deductions are disabled. Only positive awards are allowed.")

        # 4. Pre-flight Weekly Allowance Check
        if points > 0:
            allowance = await self.get_admin_allowance(admin_slack_id)
            if 'error' in allowance:
                raise PermissionError(allowance['error'])
            remaining = allowance.get('remaining', 0)
            if remaining <= 0:
                raise ValueError(
                    f"You've used your full weekly allowance ({allowance.get('allowance', 0)} pts). It resets on Monday."
                )
            if points > remaining:
                raise ValueError(
                    f"You only have {remaining} pts left this week. Try awarding {remaining} or less."
                )

        payload = {
            "admin_slack_id": admin_slack_id,
            "target_slack_id": cleaned_target,
            "points": points,
            "reason": reason,
        }
        response = await self._request(
            "POST",
            f"{self._points_base}/admin/award/",
            json=payload,
            timeout=15.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def list_rewards(self, slack_user_id: Optional[str] = None) -> List[dict]:
        """List available rewards."""
        params = {}
        if slack_user_id:
            params["slack_user_id"] = slack_user_id
        response = await self._request(
            "GET",
            f"{self._points_base}/rewards/",
            params=params,
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()
    
    async def request_reward(
        self,
        slack_user_id: str,
        reward_code: str,
        quantity: int = 1,
        notes: Optional[str] = None,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None
    ) -> dict:
        """Request a reward redemption."""
        payload = {
            "slack_user_id": slack_user_id,
            "reward_code": reward_code,
            "quantity": quantity,
        }
        if notes:
            payload["notes"] = notes
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts

        response = await self._request(
            "POST",
            f"{self._points_base}/rewards/request/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
        )
        response.raise_for_status()
        return response.json()

    async def create_points_request(
        self,
        requester_slack_id: str,
        target_slack_id: str,
        points: int,
        reason: str,
        slack_channel_id: Optional[str] = None,
        slack_thread_ts: Optional[str] = None,
    ) -> dict:
        """Create a pending points request."""
        payload = {
            "requester_slack_id": requester_slack_id,
            "target_slack_id": self._clean_slack_id(target_slack_id),
            "points": points,
            "reason": reason,
        }
        if slack_channel_id:
            payload["slack_channel_id"] = slack_channel_id
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts

        endpoint = f"{self._points_base}/requests/"

        try:
            response = await self._request(
                "POST",
                endpoint,
                json=payload,
                timeout=10.0,
                circuit_breaker=True,
            )
        except Exception as exc:
            self._log_points_request_transport_error(
                step="create_points_request",
                endpoint=endpoint,
                exc=exc,
            )
            raise

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            self._log_points_request_step(
                step="create_points_request",
                endpoint=endpoint,
                status=response.status_code,
                error_body=self._extract_response_body(response),
            )
            raise

        self._log_points_request_step(
            step="create_points_request",
            endpoint=endpoint,
            status=response.status_code,
        )
        return response.json()

    async def create_points_purchase(
        self,
        slack_user_id: str,
        pack_id: Optional[str] = None,
        points_amount: Optional[int] = None,
        purchase_from: Optional[dict] = None,
    ) -> dict:
        """Create a pending Roo Points top-up purchase."""
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "purchase_from": {"source": "slack", **(purchase_from or {})},
        }
        if pack_id:
            payload["pack_id"] = pack_id
        if points_amount is not None:
            payload["points_amount"] = points_amount

        response = await self._request(
            "POST",
            f"{self._points_base}/purchases/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def create_points_checkout_options(
        self,
        slack_user_id: str,
        *,
        checkout_request_id: str,
        pack_ids: Optional[list[str]] = None,
        purchase_from: Optional[dict] = None,
    ) -> dict:
        """Create idempotent Stripe-hosted checkout options for Roo in Slack."""
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "checkout_request_id": str(checkout_request_id or "").strip(),
            "purchase_from": {"source": "slack", **(purchase_from or {})},
        }
        if pack_ids is not None:
            payload["pack_ids"] = list(pack_ids)

        response = await self._request(
            "POST",
            f"{self._points_base}/purchases/checkout-options/",
            json=payload,
            timeout=30.0,
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def attach_points_request_slack_summary(
        self,
        request_id: int,
        slack_channel_id: str,
        slack_thread_ts: Optional[str],
        slack_summary_message_ts: str,
    ) -> dict:
        """Attach the Roo summary message identifiers to a points request."""
        payload = {
            "slack_channel_id": slack_channel_id,
            "slack_summary_message_ts": slack_summary_message_ts,
        }
        if slack_thread_ts:
            payload["slack_thread_ts"] = slack_thread_ts

        endpoint = f"{self._points_base}/requests/{request_id}/slack-summary/"

        try:
            response = await self._request(
                "PATCH",
                endpoint,
                json=payload,
                timeout=10.0,
                circuit_breaker=True,
                use_admin_headers=True,
            )
        except Exception as exc:
            self._log_points_request_transport_error(
                step="attach_points_request_slack_summary",
                endpoint=endpoint,
                exc=exc,
            )
            raise

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            self._log_points_request_step(
                step="attach_points_request_slack_summary",
                endpoint=endpoint,
                status=response.status_code,
                error_body=self._extract_response_body(response),
            )
            raise

        self._log_points_request_step(
            step="attach_points_request_slack_summary",
            endpoint=endpoint,
            status=response.status_code,
        )
        return response.json()

    async def get_points_request_by_slack_message(
        self,
        slack_channel_id: str,
        slack_message_ts: str,
    ) -> Optional[dict]:
        """Look up a points request by the Roo summary message it is attached to."""
        endpoint = f"{self._points_base}/requests/by-slack-message/"

        try:
            response = await self._request(
                "GET",
                endpoint,
                params={
                    "slack_channel_id": slack_channel_id,
                    "slack_message_ts": slack_message_ts,
                },
                timeout=10.0,
                circuit_breaker=True,
                use_admin_headers=True,
            )
        except Exception as exc:
            self._log_points_request_transport_error(
                step="get_points_request_by_slack_message",
                endpoint=endpoint,
                exc=exc,
            )
            raise

        if response.status_code == 404:
            self._log_points_request_step(
                step="get_points_request_by_slack_message",
                endpoint=endpoint,
                status=response.status_code,
                error_body=self._extract_response_body(response),
            )
            return None

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            self._log_points_request_step(
                step="get_points_request_by_slack_message",
                endpoint=endpoint,
                status=response.status_code,
                error_body=self._extract_response_body(response),
            )
            raise

        self._log_points_request_step(
            step="get_points_request_by_slack_message",
            endpoint=endpoint,
            status=response.status_code,
        )
        return response.json()

    async def approve_points_request(self, request_id: int, admin_slack_id: str) -> dict:
        """Approve a pending points request."""
        endpoint = f"{self._points_base}/requests/{request_id}/approve/"

        try:
            response = await self._request(
                "POST",
                endpoint,
                json={"admin_slack_id": admin_slack_id},
                timeout=15.0,
                circuit_breaker=True,
                use_admin_headers=True,
            )
        except Exception as exc:
            self._log_points_request_transport_error(
                step="approve_points_request",
                endpoint=endpoint,
                exc=exc,
            )
            raise

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            self._log_points_request_step(
                step="approve_points_request",
                endpoint=endpoint,
                status=response.status_code,
                error_body=self._extract_response_body(response),
            )
            raise

        self._log_points_request_step(
            step="approve_points_request",
            endpoint=endpoint,
            status=response.status_code,
        )
        return response.json()

    async def approve_reward(self, admin_slack_id: str, redemption_id: str) -> dict:
        """Approve a reward redemption request (admin only)."""
        payload = {
            "slack_user_id": admin_slack_id,
            "redemption_id": redemption_id,
        }
        response = await self._request(
            "POST",
            f"{self._points_base}/rewards/approve/",
            json=payload,
            timeout=10.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def system_award_points(
        self,
        admin_slack_id: str,
        target_slack_id: str,
        points: int,
        reason: str,
        idempotency_key: Optional[str] = None,
    ) -> dict:
        """System award points (bypasses client-side admin checks)."""
        payload = {
            "created_by_slack_id": admin_slack_id,
            "target_slack_id": self._clean_slack_id(target_slack_id),
            "points": points,
            "reason": reason,
        }
        if idempotency_key:
            payload["idempotency_key"] = str(idempotency_key)
        response = await self._request(
            "POST",
            f"{self._points_base}/system/award/",
            json=payload,
            timeout=15.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def admit_boost_post(
        self,
        *,
        submission_key: str,
        workspace_id: str,
        channel_id: str,
        root_message_ts: str,
        poster_slack_id: str,
        root_text: str,
        social_post_url: Optional[str] = None,
        timeout: float = 30.0,
        recheck_insufficient_points: bool = False,
    ) -> dict:
        """Atomically price and debit one boost post in mlai-backend."""

        payload = {
            "submission_key": str(submission_key),
            "workspace_id": str(workspace_id),
            "channel_id": str(channel_id),
            "root_message_ts": str(root_message_ts),
            "poster_slack_id": self._clean_slack_id(poster_slack_id),
            "root_text": str(root_text or ""),
            "social_post_url": str(social_post_url or ""),
            "source": "roo_slack_event",
            "recheck_insufficient_points": bool(recheck_insufficient_points),
        }
        response = await self._request(
            "POST",
            f"{self._points_base}/boost-posts/admissions/",
            json=payload,
            timeout=max(1.0, float(timeout)),
            circuit_breaker=True,
        )
        self._raise_for_status_or_backend_unavailable(response)
        return response.json()

    async def award_first_channel_post(
        self,
        slack_user_id: str,
        channel_id: str,
    ) -> dict:
        """Award the one-time intro bonus for a user's first channel post."""
        endpoint = f"{self._points_base}/activity/first-post-award/"
        payload = {
            "slack_user_id": self._clean_slack_id(slack_user_id),
            "channel_id": channel_id,
        }

        response = await self._request(
            "POST",
            endpoint,
            json=payload,
            timeout=15.0,
            circuit_breaker=True,
            use_admin_headers=True,
        )
        response.raise_for_status()
        return response.json()

    async def link_slack_user(self, slack_id: str, email: str) -> Optional[int]:
        """Link a Slack ID to an existing user found by email."""
        response = await self._request(
            "POST",
            "/api/v1/users/link-slack/",
            json={"slack_id": slack_id, "email": email},
            timeout=10.0,
            circuit_breaker=True,
            # Identity linking uses Roo's dedicated service credential rather
            # than the broader legacy internal/admin credential.
            use_admin_headers=False,
        )
        if response.status_code == 404:
            return None
        self._raise_for_status_or_backend_unavailable(response)
        payload = response.json()
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        if user_id is None:
            raise ValueError("MLAI backend omitted the linked user ID")
        return int(user_id)

    # --- MedHack Game State ---

    async def medhack_get_current_case(self) -> Optional[dict]:
        """Get the currently active MedHack case."""
        try:
            response = await self._request(
                "GET",
                "/api/v1/medhack/cases/current/",
                timeout=10.0,
                circuit_breaker=True,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"⚠️ MedHack get current case error: {e}")
            return None

    async def medhack_start_case(self, case_id: int, admin_slack_id: str) -> Optional[dict]:
        """Start a new MedHack case (admin only). Closes any active case."""
        try:
            response = await self._request(
                "POST",
                "/api/v1/medhack/cases/start/",
                json={"case_id": case_id, "admin_slack_id": admin_slack_id},
                timeout=10.0,
                circuit_breaker=True,
                use_admin_headers=True,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 403:
                print(f"⚠️ MedHack start case: not authorized")
                return None
            raise
        except Exception as e:
            print(f"⚠️ MedHack start case error: {e}")
            return None

    async def medhack_get_user_status(self, slack_user_id: str) -> Optional[dict]:
        """Get a user's status for the active MedHack case."""
        try:
            response = await self._request(
                "GET",
                f"/api/v1/medhack/cases/active/user/{slack_user_id}/",
                timeout=10.0,
                circuit_breaker=True,
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"⚠️ MedHack get user status error: {e}")
            return None

    async def medhack_set_pending_guess(self, case_id: int, slack_user_id: str, guess: str) -> Optional[dict]:
        """Store a pending guess awaiting user confirmation."""
        try:
            response = await self._request(
                "POST",
                "/api/v1/medhack/guesses/pending/",
                json={"case_id": case_id, "slack_user_id": slack_user_id, "guess": guess},
                timeout=10.0,
                circuit_breaker=True,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"⚠️ MedHack set pending guess error: {e}")
            return None

    async def medhack_clear_pending_guess(self, case_id: int, slack_user_id: str) -> bool:
        """Clear a user's pending guess."""
        try:
            response = await self._request(
                "DELETE",
                "/api/v1/medhack/guesses/pending/",
                json={"case_id": case_id, "slack_user_id": slack_user_id},
                timeout=10.0,
                circuit_breaker=True,
            )
            return response.status_code == 204
        except Exception as e:
            print(f"⚠️ MedHack clear pending guess error: {e}")
            return False

    async def medhack_submit_guess(
        self, case_id: int, slack_user_id: str, guess: str, correct: bool
    ) -> Optional[dict]:
        """Submit a confirmed guess."""
        try:
            response = await self._request(
                "POST",
                "/api/v1/medhack/guesses/submit/",
                json={
                    "case_id": case_id,
                    "slack_user_id": slack_user_id,
                    "guess": guess,
                    "correct": correct,
                },
                timeout=10.0,
                circuit_breaker=True,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"⚠️ MedHack submit guess error: {e}")
            return None

    async def medhack_record_winner(
        self, case_id: int, slack_user_id: str, is_first_solver: bool
    ) -> Optional[dict]:
        """Record a winner for a MedHack case."""
        try:
            response = await self._request(
                "POST",
                f"/api/v1/medhack/cases/{case_id}/winners/",
                json={"slack_user_id": slack_user_id, "is_first_solver": is_first_solver},
                timeout=10.0,
                circuit_breaker=True,
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            print(f"⚠️ MedHack record winner error: {e}")
            return None

    async def medhack_get_case_history(self) -> list:
        """Get history of all played MedHack cases."""
        try:
            response = await self._request(
                "GET",
                "/api/v1/medhack/cases/history/",
                timeout=10.0,
                circuit_breaker=True,
            )
            response.raise_for_status()
            return response.json().get("cases", [])
        except Exception as e:
            print(f"⚠️ MedHack get case history error: {e}")
            return []

    async def medhack_create_announcement(
        self,
        title: str,
        body: str,
        requester_slack_id: str,
        author_slack_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Create an announcement on the MedHack: Frontiers website.

        Args:
            title: Announcement title
            body: Announcement body text
            requester_slack_id: Slack ID of the human requesting the announcement
            author_slack_id: Optional Slack ID to display as the author

        Returns:
            Response dict on success (201), None on error
        """
        try:
            response = await self._request(
                "POST",
                "/api/v1/medhack/announcements/",
                json={
                    "title": title,
                    "body": body,
                    "requester_slack_id": requester_slack_id,
                    "author_slack_id": author_slack_id,
                },
                timeout=10.0,
                circuit_breaker=True,
            )
            if response.status_code == 201:
                return response.json()
            return {"status_code": response.status_code, "detail": response.text}
        except Exception as e:
            print(f"⚠️ MedHack create announcement error: {e}")
            return None

    async def healthhack_create_announcement(
        self,
        *,
        title: str,
        body: str,
        requester_slack_id: str,
        author_slack_id: Optional[str],
        source_channel_id: str,
        source_message_ts: str,
    ) -> Optional[dict]:
        """Create one idempotent HealthHack app announcement via mlai-backend."""
        try:
            response = await self._request(
                "POST",
                "/api/v1/hackathons/hospital/announcements/",
                json={
                    "title": title,
                    "body": body,
                    "requester_slack_id": requester_slack_id,
                    "author_slack_id": author_slack_id,
                    "source_channel_id": source_channel_id,
                    "source_message_ts": source_message_ts,
                },
                timeout=10.0,
                circuit_breaker=True,
            )
            if response.status_code in (200, 201):
                return response.json()
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            return {"status_code": response.status_code, "detail": detail}
        except Exception as e:
            print(f"⚠️ HealthHack create announcement error: {e}")
            return None

    async def generic_hackathon_create_announcement(
        self,
        slug: str,
        title: str,
        body: str,
        requester_slack_id: str,
        author_slack_id: Optional[str] = None,
    ) -> Optional[dict]:
        """Create an announcement on a generic hackathon website (e.g. Watt The Hack).

        Args:
            slug: Hackathon slug (e.g. "watt-the-hack")
            title: Announcement title
            body: Announcement body text
            requester_slack_id: Slack ID of the human requesting the announcement.
                The backend authorises this against Django superusers.
            author_slack_id: Slack ID to attribute as the author (Roo's bot id).

        Returns:
            Response dict on success (201), an error dict with ``status_code`` on
            a non-201 response, or None on a transport error.
        """
        try:
            response = await self._request(
                "POST",
                f"/api/v1/hackathons/{slug}/app/announcements/",
                json={
                    "title": title,
                    "body": body,
                    "requester_slack_id": requester_slack_id,
                    "slack_user_id": author_slack_id,
                },
                timeout=10.0,
                circuit_breaker=True,
            )
            if response.status_code == 201:
                return response.json()
            return {"status_code": response.status_code, "detail": response.text}
        except Exception as e:
            print(f"⚠️ Generic hackathon create announcement error: {e}")
            return None

    # End of MLAIBackendClient
