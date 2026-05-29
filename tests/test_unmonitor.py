from datetime import UTC, datetime

from optimizarr.arr import RadarrApi, SonarrApi
from optimizarr.config import Connection
from optimizarr.features.unmonitor.candidates import is_candidate
from optimizarr.features.unmonitor.config import UnmonitorAppConfig

NOW = datetime(2026, 5, 28, tzinfo=UTC)


def _radarr():
    return RadarrApi(Connection(name="radarr", url="http://x", api_key="k"))


def _sonarr():
    return SonarrApi(Connection(name="sonarr", url="http://x", api_key="k"))


def _cfg(days=30, release_type="digitalRelease", require_cutoff_met=True):
    return UnmonitorAppConfig(
        days=days, release_type=release_type, require_cutoff_met=require_cutoff_met
    )


def _movie(**over):
    movie = {
        "id": 1,
        "monitored": True,
        "hasFile": True,
        "movieFile": {"qualityCutoffNotMet": False, "dateAdded": "2026-01-01T00:00:00Z"},
        "digitalRelease": "2026-01-01T00:00:00Z",
    }
    movie.update(over)
    return movie


def test_not_monitored_is_skipped():
    ok, reason = is_candidate(_radarr(), _movie(monitored=False), _cfg(), NOW)
    assert not ok and reason == "not monitored"


def test_no_file_is_skipped():
    ok, reason = is_candidate(_radarr(), _movie(hasFile=False), _cfg(), NOW)
    assert not ok and reason == "no file"


def test_cutoff_not_met_is_skipped_when_required():
    movie = _movie(movieFile={"qualityCutoffNotMet": True, "dateAdded": "2026-01-01T00:00:00Z"})
    ok, reason = is_candidate(_radarr(), movie, _cfg(require_cutoff_met=True), NOW)
    assert not ok and reason == "quality cutoff not met"


def test_cutoff_ignored_when_not_required():
    movie = _movie(movieFile={"qualityCutoffNotMet": True, "dateAdded": "2026-01-01T00:00:00Z"})
    ok, _ = is_candidate(_radarr(), movie, _cfg(require_cutoff_met=False), NOW)
    assert ok  # old enough and has file; cutoff not checked


def test_too_recent_is_skipped():
    movie = _movie(digitalRelease="2026-05-20T00:00:00Z")
    ok, reason = is_candidate(_radarr(), movie, _cfg(days=14), NOW)
    assert not ok and reason.startswith("only")


def test_unknown_date_is_skipped():
    movie = _movie(digitalRelease=None)
    del movie["digitalRelease"]
    ok, reason = is_candidate(_radarr(), movie, _cfg(), NOW)
    assert not ok and reason == "no digitalRelease date"


def test_old_enough_is_candidate():
    ok, reason = is_candidate(_radarr(), _movie(), _cfg(days=30), NOW)
    assert ok and reason.endswith("since digitalRelease")


def test_date_added_reads_embedded_file():
    movie = _movie(digitalRelease="2026-05-27T00:00:00Z")  # recent release date
    ok, _ = is_candidate(_radarr(), movie, _cfg(release_type="dateAdded", days=30), NOW)
    assert ok  # movieFile.dateAdded is old enough


def test_sonarr_episode_shape():
    ep = {
        "id": 9,
        "monitored": True,
        "hasFile": True,
        "episodeFile": {"qualityCutoffNotMet": False},
        "airDateUtc": "2026-01-01T00:00:00Z",
    }
    ok, reason = is_candidate(_sonarr(), ep, _cfg(release_type="airDateUtc", days=30), NOW)
    assert ok and reason.endswith("since airDateUtc")
