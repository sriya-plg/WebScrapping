"""Backward-compatible carrier profile import.

Carrier profiles live beside their adapters in ``app.carriers.<carrier>``.
New carrier behavior should be registered in ``app.carriers.registry`` rather
than added to this file.
"""
from app.carriers.registry import CARRIER_PROFILES, get_carrier_profile

__all__ = ["CARRIER_PROFILES", "get_carrier_profile"]
