"""Carrier-specific behavior and profile registry.

Each carrier owns its profile and adapter in ``app.carriers.<carrier>``.
Runtime URLs, credentials, activation and schedules remain owned by the backend
API.
"""
from app.carriers.registry import CARRIER_PROFILES, get_carrier_profile

__all__ = ["CARRIER_PROFILES", "get_carrier_profile"]
