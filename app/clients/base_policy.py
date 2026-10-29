"""Optional client-specific business policy hooks.

Client assignments and BillTo values are runtime backend data, not Python config.
Override only when a client has a genuine business-rule difference.
"""
from __future__ import annotations
from typing import Any
from app.domain.job_context import JobContext


class BaseClientPolicy:
    def customize_payload(self, payload: dict[str, Any], context: JobContext) -> dict[str, Any]:
        return payload
