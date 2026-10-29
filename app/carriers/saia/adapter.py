"""Saia adapter entry point. Saia-specific interactions live in its carrier profile/hooks."""
from app.carriers.base_adapter import ConfiguredCarrierAdapter


class SaiaAdapter(ConfiguredCarrierAdapter):
    """Reusable for every client/account configured to use Saia."""
