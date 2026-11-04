""".env loading: local config works without a wrapper process; real env wins."""
import os

from app.settings import load_env_file, load_resolver


def test_env_file_sets_missing_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("SAIA_BROWSER_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("SAIA_BROWSER_MODE=scrapling\n", encoding="utf-8")

    load_env_file(env_file)

    assert os.environ["SAIA_BROWSER_MODE"] == "scrapling"
    spec = load_resolver().resolve("134034", "SAI001")
    assert spec is not None
    assert spec.browser.browser_mode == "scrapling"


def test_env_file_does_not_override_existing_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SAIA_BROWSER_MODE", "patchright")  # deployment config wins
    env_file = tmp_path / ".env"
    env_file.write_text("SAIA_BROWSER_MODE=scrapling\n", encoding="utf-8")

    load_env_file(env_file)

    assert os.environ["SAIA_BROWSER_MODE"] == "patchright"


def test_missing_env_file_is_a_noop(tmp_path):
    load_env_file(tmp_path / "does-not-exist.env")  # must not raise


def test_keys_without_values_in_log(tmp_path, monkeypatch, caplog):
    import logging
    caplog.set_level(logging.INFO)
    monkeypatch.delenv("SAIA_BROWSER_MODE", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("SAIA_BROWSER_MODE=scrapling\n", encoding="utf-8")

    load_env_file(env_file)

    assert any("newly_set_keys=['SAIA_BROWSER_MODE']" in rec.message for rec in caplog.records)
    assert not any("scrapling" in rec.message for rec in caplog.records), "values must not be logged"