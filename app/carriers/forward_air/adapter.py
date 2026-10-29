"""Forward Air adapter entry point; preserves configured batch tracking behavior."""
from app.carriers.base_adapter import ConfiguredCarrierAdapter


class ForwardAirAdapter(ConfiguredCarrierAdapter):
    """Reusable for every client/account configured to use Forward Air."""
