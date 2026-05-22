from datetime import UTC, datetime

from stalearr.config import AppConfig
from stalearr.radarr import _is_candidate


def _config(**overrides) -> AppConfig:
    base = {
        "name": "radarr",
        "url": "http://radarr",
        "api_key": "k",
        "days": 30,
        "release_type": "digitalRelease",
        "require_cutoff_met": True,
    }
    base.update(overrides)
    return AppConfig(**base)


NOW = datetime(2026, 5, 22, tzinfo=UTC)


def test_old_release_with_cutoff_met_is_candidate():
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "digitalRelease": "2026-01-01T00:00:00Z",
        "movieFile": {"qualityCutoffNotMet": False, "dateAdded": "2026-01-02T00:00:00Z"},
    }
    ok, _ = _is_candidate(movie, _config(), NOW)
    assert ok


def test_unmonitored_movie_skipped():
    movie = {
        "id": 1,
        "monitored": False,
        "hasFile": True,
        "digitalRelease": "2020-01-01T00:00:00Z",
        "movieFile": {"qualityCutoffNotMet": False},
    }
    ok, reason = _is_candidate(movie, _config(), NOW)
    assert not ok
    assert "not monitored" in reason


def test_cutoff_not_met_skipped_when_required():
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "digitalRelease": "2020-01-01T00:00:00Z",
        "movieFile": {"qualityCutoffNotMet": True},
    }
    ok, reason = _is_candidate(movie, _config(), NOW)
    assert not ok
    assert "cutoff" in reason


def test_cutoff_not_met_allowed_when_not_required():
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "digitalRelease": "2020-01-01T00:00:00Z",
        "movieFile": {"qualityCutoffNotMet": True},
    }
    ok, _ = _is_candidate(movie, _config(require_cutoff_met=False), NOW)
    assert ok


def test_no_file_always_skipped():
    # A wanted-but-undownloaded movie must never be unmonitored, regardless of cutoff toggle.
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": False,
        "digitalRelease": "2020-01-01T00:00:00Z",
    }
    for require in (True, False):
        ok, reason = _is_candidate(movie, _config(require_cutoff_met=require), NOW)
        assert not ok
        assert "no file" in reason


def test_recent_release_skipped():
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "digitalRelease": "2026-05-10T00:00:00Z",  # 12 days ago, threshold 30
        "movieFile": {"qualityCutoffNotMet": False},
    }
    ok, reason = _is_candidate(movie, _config(), NOW)
    assert not ok
    assert "only" in reason


def test_missing_release_date_skipped():
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "digitalRelease": None,
        "movieFile": {"qualityCutoffNotMet": False},
    }
    ok, reason = _is_candidate(movie, _config(), NOW)
    assert not ok
    assert "no digitalRelease" in reason


def test_date_added_release_type_reads_movie_file():
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "digitalRelease": "2026-05-21T00:00:00Z",  # recent, would skip
        "movieFile": {
            "qualityCutoffNotMet": False,
            "dateAdded": "2026-01-01T00:00:00Z",  # old enough
        },
    }
    ok, _ = _is_candidate(movie, _config(release_type="dateAdded"), NOW)
    assert ok
