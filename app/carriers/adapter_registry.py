"""Resolve a reusable adapter by carrier code, never by client name."""
from __future__ import annotations

from app.browser.base import BrowserProvider
from app.carriers.base_adapter import BaseCarrierAdapter, ConfiguredCarrierAdapter
from app.carriers.estes.adapter import EstesAdapter
from app.carriers.forward_air.adapter import ForwardAirAdapter
from app.carriers.saia.adapter import SaiaAdapter
from app.settings import CarrierSpec


class CarrierAdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, type[BaseCarrierAdapter]] = {
            "SAIA": SaiaAdapter,
            "ESTES": EstesAdapter,
            "FORWARDAIR": ForwardAirAdapter,
        }

    def register(self, carrier_code: str, adapter: type[BaseCarrierAdapter]) -> None:
        self._adapters[carrier_code.strip().upper().replace("_", "").replace(" ", "")] = adapter

    def create(self, provider: BrowserProvider, spec: CarrierSpec, debug_dir: str) -> BaseCarrierAdapter:
        key = spec.code.strip().upper().replace("_", "").replace(" ", "")
        adapter_cls = self._adapters.get(key, ConfiguredCarrierAdapter)
        return adapter_cls(provider, spec, debug_dir)


carrier_adapter_registry = CarrierAdapterRegistry()
