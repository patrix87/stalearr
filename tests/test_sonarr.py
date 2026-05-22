from datetime import UTC, datetime

from stalearr.config import AppConfig
from stalearr.sonarr import _is_candidate


def _config(**overrides) -> AppConfig:
    base = {
        "name": "sonarr",
        "url": "http://sonarr",
        "api_key": "k",
        "days": 30,
        "release_type": "airDateUtc",
        "require_cutoff_met": True,
    }
    base.update(overrides)
    return AppConfig(**base)


NOW = datetime(2026, 5, 22, tzinfo=UTC)


def test_old_aired_episode_with_cutoff_met_is_candidate():
    episode = {
        "id": 10,
        "monitored": True,
        "hasFile": True,
        "airDateUtc": "2026-01-01T00:00:00Z",
        "episodeFile": {"qualityCutoffNotMet": False, "dateAdded": "2026-01-02T00:00:00Z"},
    }
    ok, _ = _is_candidate(episode, _config(), NOW)
    assert ok


def test_unmonitored_episode_skipped():
    episode = {"id": 1, "monitored": False, "airDateUtc": "2020-01-01T00:00:00Z"}
    ok, reason = _is_candidate(episode, _config(), NOW)
    assert not ok
    assert "not monitored" in reason


def test_recent_episode_skipped():
    episode = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "airDateUtc": "2026-05-15T00:00:00Z",  # 7 days
        "episodeFile": {"qualityCutoffNotMet": False},
    }
    ok, reason = _is_candidate(episode, _config(), NOW)
    assert not ok
    assert "only" in reason


def test_cutoff_not_met_skipped_when_required():
    episode = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "airDateUtc": "2020-01-01T00:00:00Z",
        "episodeFile": {"qualityCutoffNotMet": True},
    }
    ok, reason = _is_candidate(episode, _config(), NOW)
    assert not ok
    assert "cutoff" in reason


def test_cutoff_not_met_allowed_when_not_required():
    episode = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "airDateUtc": "2020-01-01T00:00:00Z",
        "episodeFile": {"qualityCutoffNotMet": True},
    }
    ok, _ = _is_candidate(episode, _config(require_cutoff_met=False), NOW)
    assert ok


def test_no_file_always_skipped():
    episode = {
        "id": 1,
        "monitored": True,
        "hasFile": False,
        "airDateUtc": "2020-01-01T00:00:00Z",
    }
    for require in (True, False):
        ok, reason = _is_candidate(episode, _config(require_cutoff_met=require), NOW)
        assert not ok
        assert "no file" in reason


def test_date_added_release_type_uses_episode_file():
    episode = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "airDateUtc": "2026-05-21T00:00:00Z",
        "episodeFile": {
            "qualityCutoffNotMet": False,
            "dateAdded": "2026-01-01T00:00:00Z",
        },
    }
    ok, _ = _is_candidate(episode, _config(release_type="dateAdded"), NOW)
    assert ok
