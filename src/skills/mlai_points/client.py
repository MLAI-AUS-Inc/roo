"""
Compatibility shim for the canonical Roo MLAI backend client.

Points request and approval behavior now lives in `roo.clients.mlai_backend`
so Roo has a single source of truth for the points API contract.
"""

from roo.clients.mlai_backend import MLAIBackendClient

__all__ = ["MLAIBackendClient"]
