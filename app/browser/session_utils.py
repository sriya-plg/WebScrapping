"""Shared browser-session helpers for the single batch scraping pipeline."""
from app.settings import CarrierSpec

DEFAULT_CONSENT_SELECTORS = [
    "#onetrust-reject-all-handler",
    "#CybotCookiebotDialogBodyButtonDecline",
    "button:has-text('Reject All')",
    "button:has-text('Reject all')",
    "button:has-text('Decline')",
    "button:has-text('Do Not Sell')",
    "button:has-text('Do not sell')",
]


def session_key(spec: CarrierSpec) -> str:
    """One storage-state file per built-in client/carrier profile."""
    return f"{spec.client}_{spec.code}"
