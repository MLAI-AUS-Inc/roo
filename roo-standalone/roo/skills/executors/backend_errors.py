"""Backend errors skill execution."""
from __future__ import annotations
import httpx


class BackendErrors:
    def _extract_luma_http_error_detail(self, exc: httpx.HTTPStatusError) -> str:
        try:
            payload = exc.response.json()
        except ValueError:
            return self._extract_http_error_detail(exc)
        except Exception:
            return ""

        if isinstance(payload, dict):
            for key in ("error", "detail", "message"):
                value = payload.get(key)
                if value:
                    return str(value)
        if exc.response.status_code >= 500:
            return self._extract_http_error_detail(exc)
        if payload in (None, "", [], {}):
            return ""
        return str(payload)


    def _extract_http_error_detail(self, exc: httpx.HTTPStatusError) -> str:
        """Extract a compact error string from a backend HTTP error."""
        if exc.response.status_code >= 500:
            return "MLAI backend is temporarily unavailable. Please try again in a moment."

        try:
            payload = exc.response.json()
        except ValueError:
            body = exc.response.text.strip()
            body_lower = body.lower()
            content_type = exc.response.headers.get("content-type", "").lower()
            if "<!doctype html" in body_lower or "<html" in body_lower or "html" in content_type:
                return "MLAI backend returned an unexpected error response. Please try again in a moment."
            return body
        except Exception:
            return ""

        if isinstance(payload, dict):
            for key in ("error", "detail", "message"):
                value = payload.get(key)
                if value:
                    return str(value)
        if payload in (None, "", [], {}):
            return ""
        return str(payload)


