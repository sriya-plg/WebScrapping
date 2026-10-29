"""Core architecture tests for backend-owned configuration and database-free scheduling."""
from __future__ import annotations

from types import SimpleNamespace
import time

from app.models import CarrierConfig
from app.settings import AppSettings, load_resolver, load_settings
from app.core.runner import Runner


def test_builtin_profiles_resolve_aliases_without_local_files():
    resolver = load_resolver("this/path/is/intentionally/ignored")
    saia = resolver.resolve("134034", "SAI001")
    estes = resolver.resolve("134034", "ESTE03")
    forward = resolver.resolve("134034", "FORW-LTL")
    assert saia and saia.code == "SAIA"
    assert estes and estes.code == "ESTES"
    assert forward and forward.code == "FORWARDAIR"
    assert saia.site.tracking_url is None  # runtime URL must come from the backend


def test_backend_runtime_values_override_only_runtime_fields():
    resolver = load_resolver()
    config = CarrierConfig(
        id=1, carrierName="Saia", carrierCode="SAI001", trackingUrl="https://backend.example/track",
        billTo=134034, customerName="Calix", processingFrequency=1439, processingDelay=2, isActive=True,
    )
    spec = resolver.resolve(str(config.bill_to), config.carrier_code, config=config)
    assert spec.site.tracking_url == "https://backend.example/track"
    assert spec.site.inter_pro_delay_s >= 2
    assert spec.site.input_selector == "#trackingNumbers"  # stable UI behavior stays in code


def test_frequency_is_minutes_and_only_process_local():
    settings = AppSettings()
    runner = Runner(SimpleNamespace(settings=settings), load_resolver())
    config = CarrierConfig(
        id=1, carrierName="Saia", carrierCode="SAI001", trackingUrl="https://example.test",
        billTo=134034, customerName="Calix", processingFrequency=1439, isActive=True,
    )
    spec = load_resolver().resolve("134034", "SAI001", config=config)
    assert runner._due(config, spec, force=False) is True
    runner._last_run[(spec.client, spec.code, config.bill_to)] = time.time()
    assert runner._due(config, spec, force=False) is False
    assert runner._due(config, spec, force=True) is True
    config.processing_frequency = 0
    assert runner._due(config, spec, force=False) is True


def test_settings_load_from_environment_not_external_yaml(monkeypatch):
    monkeypatch.setenv("TRACKER_BASE_URL", "https://backend.example")
    monkeypatch.setenv("RUN_INTERVAL_HOURS", "4")
    settings = load_settings("nonexistent-config.yaml")
    assert settings.backend.base_url == "https://backend.example"
    assert settings.schedule.interval_hours == 4
    assert settings.browser_state_dir.startswith("app/runtime/")
    assert not hasattr(settings, "state_db")
