"""ScraplingProvider: launch kwargs, lock guard, and patchright fallback paths."""
import asyncio
import os
import time
from pathlib import Path

import pytest

import app.browser.scrapling_provider as scrapling_provider
from app.browser.scrapling_provider import LOCK_STALE_AFTER_S, ScraplingProvider, _safe_name
from app.settings import BrowserConfig


class FakePage:
    def __init__(self): self.listeners = {}

    def set_default_timeout(self, ms): self.timeout_ms = ms

    def on(self, event, handler): self.listeners[event] = handler


class FakeContext:
    def __init__(self): self.page = FakePage(); self.closed = False

    async def new_page(self): return self.page

    async def close(self): self.closed = True


class FakeEngine:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.context = FakeContext()
        FakeEngine.instances.append(self)

    async def start(self):
        if self.kwargs.get("fail_start"):
            raise RuntimeError("engine launch boom")
        self.started = True

    async def close(self): self.closed = True


class FakeFallbackSession:
    engine_name = "patchright"
    state_reused = False

    async def close(self): pass


class FakeFallbackProvider:
    def __init__(self):
        self.opened = []
        self.closed = False

    async def open_session(self, key):
        self.opened.append(key)
        return FakeFallbackSession()

    async def close(self): self.closed = True


def make_cfg(**overrides):
    values = dict(browser_mode="scrapling", headless=False, channel="chrome",
                  locale="en-US", timezone_id="America/Chicago",
                  viewport_w=1366, viewport_h=820, nav_timeout_s=45)
    values.update(overrides)
    return BrowserConfig(**values)


@pytest.fixture(autouse=True)
def _fresh_engine_instances():
    FakeEngine.instances = []


def test_kwargs_map_from_browser_config(tmp_path, monkeypatch):
    monkeypatch.setattr(scrapling_provider, "_load_stealthy_session", lambda: FakeEngine)
    provider = ScraplingProvider(make_cfg(), str(tmp_path), fallback=FakeFallbackProvider())
    session = asyncio.run(provider.open_session("CALIX NETWORKS_SAIA"))
    engine = FakeEngine.instances[0]
    key = "CALIX NETWORKS_SAIA"
    expected_dir = str(tmp_path / _safe_name(f"scrapling_{key}"))
    assert engine.kwargs["user_data_dir"] == expected_dir
    assert engine.kwargs["headless"] is False
    assert engine.kwargs["real_chrome"] is True  # channel "chrome"
    assert engine.kwargs["timeout"] == 45_000
    assert engine.kwargs["locale"] == "en-US"
    assert engine.kwargs["timezone_id"] == "America/Chicago"
    assert engine.kwargs["additional_args"]["viewport"] == {"width": 1366, "height": 820}
    assert session.engine_name == "scrapling"
    assert session.state_reused is False

    # Scrapling passthrough options win; additional_args merges with the built-ins.
    asyncio.run(provider.close())  # release the profile lock for the second provider
    cfg = make_cfg(scrapling_options={"block_ads": True, "additional_args": {"extra_flag": 1}})
    provider = ScraplingProvider(cfg, str(tmp_path), fallback=FakeFallbackProvider())
    session2 = asyncio.run(provider.open_session(key))
    kw = FakeEngine.instances[-1].kwargs
    assert kw["block_ads"] is True
    assert kw["additional_args"]["extra_flag"] == 1
    assert kw["additional_args"]["viewport"] == {"width": 1366, "height": 820}
    assert session2.engine_name == "scrapling"


def test_existing_profile_is_reused_and_not_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(scrapling_provider, "_load_stealthy_session", lambda: FakeEngine)
    key = "CALIX NETWORKS_SAIA"
    profile_dir = tmp_path / _safe_name(f"scrapling_{key}")
    profile_dir.mkdir(parents=True)
    marker = profile_dir / "profile-marker"
    marker.write_text("persisted", encoding="utf-8")
    provider = ScraplingProvider(make_cfg(), str(tmp_path), fallback=FakeFallbackProvider())

    session = asyncio.run(provider.open_session(key))

    assert session.state_reused is True
    assert FakeEngine.instances[-1].kwargs["user_data_dir"] == str(profile_dir)
    asyncio.run(provider.close())
    assert marker.read_text(encoding="utf-8") == "persisted"


def test_headless_env_override(tmp_path, monkeypatch):
    monkeypatch.setattr(scrapling_provider, "_load_stealthy_session", lambda: FakeEngine)
    monkeypatch.setenv("BROWSER_HEADLESS", "true")
    provider = ScraplingProvider(make_cfg(), str(tmp_path), fallback=FakeFallbackProvider())
    asyncio.run(provider.open_session("k"))
    assert FakeEngine.instances[-1].kwargs["headless"] is True


def test_missing_extras_falls_back_to_patchright(tmp_path, monkeypatch, caplog):
    def missing():
        raise ModuleNotFoundError("No module named 'curl_cffi'")

    monkeypatch.setattr(scrapling_provider, "_load_stealthy_session", missing)
    fallback = FakeFallbackProvider()
    provider = ScraplingProvider(make_cfg(), str(tmp_path), fallback=fallback)
    session = asyncio.run(provider.open_session("k"))
    assert isinstance(session, FakeFallbackSession)
    assert fallback.opened == ["k"]
    assert any("extras not installed" in rec.message for rec in caplog.records)


def test_launch_failure_falls_back_with_reason(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(scrapling_provider, "_load_stealthy_session", lambda: FakeEngine)
    cfg = make_cfg(scrapling_options={"fail_start": True})  # engine.start() raises
    fallback = FakeFallbackProvider()
    provider = ScraplingProvider(cfg, str(tmp_path), fallback=fallback)
    session = asyncio.run(provider.open_session("k"))
    assert isinstance(session, FakeFallbackSession)
    assert any("failed to start" in rec.message for rec in caplog.records)
    assert any("engine launch boom" in rec.message for rec in caplog.records)


def test_locked_profile_falls_back_and_stale_lock_is_replaced(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(scrapling_provider, "_load_stealthy_session", lambda: FakeEngine)
    key = "k"
    lock = Path(str(tmp_path / _safe_name(f"scrapling_{key}")) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(99999), encoding="utf-8")

    fallback = FakeFallbackProvider()
    provider = ScraplingProvider(make_cfg(), str(tmp_path), fallback=fallback)
    session = asyncio.run(provider.open_session(key))
    # Fresh lock held by another process -> fallback with the pid in the message.
    assert isinstance(session, FakeFallbackSession)
    assert any("in use by another process" in rec.message for rec in caplog.records)
    assert any("99999" in rec.message for rec in caplog.records)

    # A stale lock (>24h) is replaced and the scrapling engine starts.
    old = time.time() - LOCK_STALE_AFTER_S - 60
    os.utime(lock, (old, old))
    session = asyncio.run(provider.open_session(key))
    assert session.engine_name == "scrapling"
    assert FakeEngine.instances, "stale lock should have been replaced"


def test_close_stops_engine_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(scrapling_provider, "_load_stealthy_session", lambda: FakeEngine)
    fallback = FakeFallbackProvider()
    provider = ScraplingProvider(make_cfg(), str(tmp_path), fallback=fallback)
    session = asyncio.run(provider.open_session("k"))
    lock = Path(str(tmp_path / _safe_name("scrapling_k")) + ".lock")
    assert lock.exists()
    asyncio.run(session.close())
    asyncio.run(provider.close())
    engine = FakeEngine.instances[0]
    assert engine.closed
    assert not lock.exists()
    assert fallback.closed
    assert engine.context.closed