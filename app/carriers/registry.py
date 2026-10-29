"""Registry for carrier-specific built-in behavior.

This registry contains stable browser/mapping behavior only. Runtime carrier
configuration (tracking URL, credentials, active flag and cadence) comes from
the backend API. Add a new carrier by adding its profile and adapter under
``app.carriers.<carrier>`` and registering the profile here.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.carriers.estes.profile import PROFILE as ESTES_PROFILE
from app.carriers.forward_air.profile import PROFILE as FORWARDAIR_PROFILE
from app.carriers.saia.profile import PROFILE as SAIA_PROFILE

# Keep the legacy dictionary-shaped API for SpecResolver and downstream code.
CARRIER_PROFILES: dict[str, dict[str, Any]] = {
    "ESTES": ESTES_PROFILE,
    "FORWARDAIR": FORWARDAIR_PROFILE,
    "SAIA": SAIA_PROFILE,
}

def get_carrier_profile(code: str) -> dict[str, Any] | None:
    """Return an isolated copy of a profile so callers cannot mutate the registry."""
    profile = CARRIER_PROFILES.get(str(code or "").strip().upper())
    return deepcopy(profile) if profile is not None else None
