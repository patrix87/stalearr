import pytest

from stalearr.config import load_config

_MANAGED_ENV_VARS = [
    "CRON_SCHEDULE",
    "RUN_ON_START",
    "DRY_RUN",
    "LOG_LEVEL",
    "RADARR_URL",
    "RADARR_API_KEY",
    "RADARR_DAYS",
    "RADARR_RELEASE_TYPE",
    "RADARR_REQUIRE_CUTOFF_MET",
    "SONARR_URL",
    "SONARR_API_KEY",
    "SONARR_DAYS",
    "SONARR_RELEASE_TYPE",
    "SONARR_REQUIRE_CUTOFF_MET",
]


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for key in _MANAGED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def test_radarr_only_with_defaults(monkeypatch):
    monkeypatch.setenv("RADARR_URL", "http://radarr:7878")
    monkeypatch.setenv("RADARR_API_KEY", "abc")

    config = load_config()
    assert config.radarr is not None
    assert config.sonarr is None
    assert config.radarr.url == "http://radarr:7878"
    assert config.radarr.api_key == "abc"
    assert config.radarr.days == 30
    assert config.radarr.release_type == "digitalRelease"
    assert config.radarr.require_cutoff_met is True
    assert config.cron_schedule == "0 4 * * *"
    assert config.run_on_start is True
    assert config.dry_run is False


def test_sonarr_only_with_overrides(monkeypatch):
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989/")
    monkeypatch.setenv("SONARR_API_KEY", "k")
    monkeypatch.setenv("SONARR_DAYS", "60")
    monkeypatch.setenv("SONARR_RELEASE_TYPE", "dateAdded")
    monkeypatch.setenv("SONARR_REQUIRE_CUTOFF_MET", "false")
    monkeypatch.setenv("CRON_SCHEDULE", "*/30 * * * *")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("RUN_ON_START", "no")

    config = load_config()
    assert config.radarr is None
    assert config.sonarr is not None
    assert config.sonarr.url == "http://sonarr:8989"  # trailing slash stripped
    assert config.sonarr.days == 60
    assert config.sonarr.release_type == "dateAdded"
    assert config.sonarr.require_cutoff_met is False
    assert config.cron_schedule == "*/30 * * * *"
    assert config.dry_run is True
    assert config.run_on_start is False


def test_rejects_when_neither_configured(monkeypatch):
    with pytest.raises(ValueError, match="Neither"):
        load_config()


def test_rejects_invalid_radarr_release_type(monkeypatch):
    monkeypatch.setenv("RADARR_URL", "http://x")
    monkeypatch.setenv("RADARR_API_KEY", "k")
    monkeypatch.setenv("RADARR_RELEASE_TYPE", "premiereDate")
    with pytest.raises(ValueError, match="RADARR_RELEASE_TYPE"):
        load_config()


def test_rejects_invalid_sonarr_release_type(monkeypatch):
    monkeypatch.setenv("SONARR_URL", "http://x")
    monkeypatch.setenv("SONARR_API_KEY", "k")
    monkeypatch.setenv("SONARR_RELEASE_TYPE", "digitalRelease")
    with pytest.raises(ValueError, match="SONARR_RELEASE_TYPE"):
        load_config()
