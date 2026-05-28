import pytest

from optimizarr.config import load_config

_MANAGED_ENV_VARS = [
    "LOG_LEVEL",
    "CONFIG_PATH",
    "STATE_PATH",
    "RADARR_URL",
    "RADARR_API_KEY",
    "SONARR_URL",
    "SONARR_API_KEY",
]


@pytest.fixture(autouse=True)
def clear_env(monkeypatch):
    for key in _MANAGED_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def _write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    return str(path)


def test_radarr_only_with_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("RADARR_URL", "http://radarr:7878")
    monkeypatch.setenv("RADARR_API_KEY", "abc")
    path = _write(tmp_path, "")

    config = load_config(path)
    assert config.radarr is not None
    assert config.sonarr is None
    assert config.radarr.url == "http://radarr:7878"
    assert config.radarr.api_key == "abc"
    assert config.dry_run is False
    assert config.state_path == "/data/state.json"

    um = config.unmonitor
    assert um.enabled is True
    assert um.cron_schedule == "0 4 * * *"
    assert um.run_on_start is True
    assert um.radarr.days == 30
    assert um.radarr.release_type == "digitalRelease"
    assert um.radarr.require_cutoff_met is True

    # apps list narrows to configured connections
    assert config.optimizer.apps == ["radarr"]
    assert config.optimizer.enabled is False


def test_overrides_from_toml(monkeypatch, tmp_path):
    monkeypatch.setenv("SONARR_URL", "http://sonarr:8989/")
    monkeypatch.setenv("SONARR_API_KEY", "k")
    path = _write(
        tmp_path,
        """
        dry_run = true

        [unmonitor]
        cron_schedule = "*/30 * * * *"
        run_on_start = false

        [unmonitor.sonarr]
        days = 60
        release_type = "dateAdded"
        require_cutoff_met = false
        """,
    )

    config = load_config(path)
    assert config.radarr is None
    assert config.sonarr is not None
    assert config.sonarr.url == "http://sonarr:8989"  # trailing slash stripped
    assert config.dry_run is True
    assert config.unmonitor.cron_schedule == "*/30 * * * *"
    assert config.unmonitor.run_on_start is False
    assert config.unmonitor.sonarr.days == 60
    assert config.unmonitor.sonarr.release_type == "dateAdded"
    assert config.unmonitor.sonarr.require_cutoff_met is False
    assert config.optimizer.apps == ["sonarr"]


def test_rejects_when_neither_configured(tmp_path):
    with pytest.raises(ValueError, match="Neither"):
        load_config(_write(tmp_path, ""))


def test_rejects_missing_config_file(monkeypatch):
    monkeypatch.setenv("RADARR_URL", "http://x")
    monkeypatch.setenv("RADARR_API_KEY", "k")
    with pytest.raises(ValueError, match="not found"):
        load_config("/nonexistent/config.toml")


def test_rejects_invalid_radarr_release_type(monkeypatch, tmp_path):
    monkeypatch.setenv("RADARR_URL", "http://x")
    monkeypatch.setenv("RADARR_API_KEY", "k")
    path = _write(tmp_path, '[unmonitor.radarr]\nrelease_type = "premiereDate"\n')
    with pytest.raises(ValueError, match="release_type"):
        load_config(path)


def test_rejects_invalid_pick_order(monkeypatch, tmp_path):
    monkeypatch.setenv("RADARR_URL", "http://x")
    monkeypatch.setenv("RADARR_API_KEY", "k")
    path = _write(tmp_path, '[optimizer]\npick_order = "sideways"\n')
    with pytest.raises(ValueError, match="pick_order"):
        load_config(path)


def test_rejects_weights_not_summing_to_one(monkeypatch, tmp_path):
    monkeypatch.setenv("RADARR_URL", "http://x")
    monkeypatch.setenv("RADARR_API_KEY", "k")
    path = _write(
        tmp_path,
        "[optimizer.topsis.weights]\nscore = 0.5\nresolution = 0.3\nsize = 0.3\n",
    )
    with pytest.raises(ValueError, match="sum to 1.0"):
        load_config(path)


def test_parses_topsis_tables(monkeypatch, tmp_path):
    monkeypatch.setenv("RADARR_URL", "http://x")
    monkeypatch.setenv("RADARR_API_KEY", "k")
    path = _write(
        tmp_path,
        """
        [optimizer]
        enabled = true
        queue_max = 2

        [optimizer.topsis]
        score_floor_preferred = 800000

        [optimizer.topsis.size_envelope_by_profile."2160p Quality"]
        "2160" = [12.0, 40.0]

        [optimizer.topsis.sanity_gbh_floor_by_resolution]
        "1080" = 0.9
        """,
    )

    config = load_config(path)
    topsis = config.optimizer.topsis
    assert config.optimizer.enabled is True
    assert config.optimizer.queue_max == 2
    assert topsis.score_floor_preferred == 800000
    assert topsis.size_envelope_by_profile["2160p Quality"][2160] == (12.0, 40.0)
    assert topsis.sanity_gbh_floor_by_resolution[1080] == 0.9
