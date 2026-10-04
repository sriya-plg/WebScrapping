"""
The registry ties each carrier's YAML config to its normalizer function.

TO ADD A NEW CARRIER:
  1. Create app/config/carriers/<name>.yaml (copy an existing one as a template)
  2. Create app/carriers/<name>/normalizer.py with a `normalize(payload, reference) -> TrackingResult` function
  3. Add one line below

No other file needs to change.
"""

from app.core.base_scraper import GenericScraper
from app.core.carrier_config import load_carrier_config
from app.core.exceptions import CarrierConfigError

from app.carriers.estes import normalizer as estes_normalizer
from app.carriers.forward_air import normalizer as forward_air_normalizer
from app.carriers.saia import normalizer as saia_normalizer

_NORMALIZERS = {
    "estes": estes_normalizer.normalize,
    "forward_air": forward_air_normalizer.normalize,
    "saia": saia_normalizer.normalize,
}

_scraper_cache: dict[str, GenericScraper] = {}


def get_scraper(carrier_name: str) -> GenericScraper:
    """Returns a (cached) GenericScraper configured for the given carrier."""
    carrier_name = carrier_name.lower()
    if carrier_name in _scraper_cache:
        return _scraper_cache[carrier_name]

    if carrier_name not in _NORMALIZERS:
        raise CarrierConfigError(
            f"Unknown carrier '{carrier_name}'. Registered: {list(_NORMALIZERS)}"
        )

    config = load_carrier_config(carrier_name)
    scraper = GenericScraper(config, _NORMALIZERS[carrier_name])
    _scraper_cache[carrier_name] = scraper
    return scraper


def registered_carriers() -> list[str]:
    return list(_NORMALIZERS)
