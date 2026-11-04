"""SAIA_BROWSER_MODE env override: Saia-only engine selection (patchright | scrapling)."""
import pytest

from app.browser.factory import make_provider
from app.browser.scrapling_provider import ScraplingProvider
from app.settings import BrowserConfig, load_resolver


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("SAIA_BROWSER_MODE", raising=False)


def test_env_mode_scrapling_selects_scrapling_for_saia(monkeypatch):
    monkeypatch.setenv("SAIA_BROWSER_MODE", "scrapling")
    resolver = load_resolver()
    spec = resolver.resolve("134034", "SAI001")
    assert spec is not None and spec.code == "SAIA"
    assert spec.browser.browser_mode == "scrapling"
    provider = make_provider(spec.browser, "app/runtime/browser")
    assert isinstance(provider, ScraplingProvider)


def test_env_mode_scrapling_does_not_affect_other_carriers(monkeypatch):
    monkeypatch.setenv("SAIA_BROWSER_MODE", "scrapling")
    resolver = load_resolver()
    estes = resolver.resolve("100", "ESTES")
    assert estes is not None
    assert estes.browser.browser_mode == "playwright_stealth"
    forward_air = resolver.resolve("100", "FORWARDAIR")
    assert forward_air is not None
    assert forward_air.browser.browser_mode == "patchright"


def test_env_mode_unset_keeps_profile_default():
    resolver = load_resolver()
    spec = resolver.resolve("134034", "SAIA")
    assert spec is not None
    assert spec.browser.browser_mode == "patchright"
    provider = make_provider(spec.browser, "app/runtime/browser")
    assert provider.__class__.__name__ == "PlaywrightProvider"
    assert provider.engine == "patchright"


def test_env_mode_invalid_value_warns_and_keeps_default(monkeypatch, caplog):
    monkeypatch.setenv("SAIA_BROWSER_MODE", "curl")
    resolver = load_resolver()
    spec = resolver.resolve("134034", "SAIA")
    assert spec is not None
    assert spec.browser.browser_mode == "patchright", "invalid value must not change the engine"
    assert any("SAIA_BROWSER_MODE" in rec.message and "invalid" in rec.message
               for rec in caplog.records)


def test_env_mode_patchright_is_explicit_noop(monkeypatch):
    monkeypatch.setenv("SAIA_BROWSER_MODE", "patchright")
    resolver = load_resolver()
    spec = resolver.resolve("134034", "SAIA")
    assert spec is not None
    assert spec.browser.browser_mode == "patchright"


def test_make_provider_rejects_unknown_mode():
    cfg = BrowserConfig()
    cfg.browser_mode = "puppeteer"  # bypass Literal validation the way a bad profile would
    with pytest.raises(ValueError):
        make_provider(cfg, "app/runtime/browser")