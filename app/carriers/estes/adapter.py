"""Estes adapter entry point; preserves the shared browser/network capture pipeline."""
from app.carriers.base_adapter import ConfiguredCarrierAdapter


class EstesAdapter(ConfiguredCarrierAdapter):
    """Reusable for every client/account configured to use Estes."""
