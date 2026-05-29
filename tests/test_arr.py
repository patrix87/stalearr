from optimizarr.arr import RadarrApi, SonarrApi, build_client, max_allowed_resolution
from optimizarr.config import Connection


def test_max_allowed_resolution_nested():
    items = [
        {"allowed": False, "quality": {"resolution": 480}},
        {
            "allowed": True,
            "items": [
                {"allowed": True, "quality": {"resolution": 1080}},
                {"allowed": True, "quality": {"resolution": 2160}},
            ],
        },
    ]
    assert max_allowed_resolution(items) == 2160


def _conn(name="radarr"):
    return Connection(name=name, url="http://x", api_key="k")


def test_build_client_picks_app():
    assert isinstance(build_client("radarr", _conn()), RadarrApi)
    assert isinstance(build_client("sonarr", _conn("sonarr")), SonarrApi)


def test_radarr_accessors():
    api = RadarrApi(_conn())
    movie = {
        "id": 7,
        "monitored": True,
        "hasFile": True,
        "movieFile": {"id": 99, "dateAdded": "2026-01-01T00:00:00Z", "qualityCutoffNotMet": False},
        "digitalRelease": "2025-12-01T00:00:00Z",
    }
    assert api.item_id(movie) == 7
    assert api.monitored(movie) is True
    assert api.has_file(movie) is True
    assert api.current_file_id(movie) == 99
    assert api.cutoff_met(movie) is True  # qualityCutoffNotMet=False -> cutoff met
    assert api.reference_date(movie, "digitalRelease") == "2025-12-01T00:00:00Z"
    assert api.reference_date(movie, "dateAdded") == "2026-01-01T00:00:00Z"


def test_radarr_cutoff_not_met_defaults_true():
    api = RadarrApi(_conn())
    assert api.cutoff_met({"movieFile": {}}) is False  # missing -> qualityCutoffNotMet True
    assert api.cutoff_met({}) is False  # no file -> not met


def test_sonarr_accessors_use_embedded_episode_file():
    api = SonarrApi(_conn("sonarr"))
    ep = {
        "id": 3,
        "monitored": False,
        "hasFile": True,
        "episodeFile": {"id": 42, "dateAdded": "2026-02-02T00:00:00Z", "qualityCutoffNotMet": True},
        "airDateUtc": "2025-01-01T00:00:00Z",
    }
    assert api.item_id(ep) == 3
    assert api.monitored(ep) is False
    assert api.current_file_id(ep) == 42
    assert api.cutoff_met(ep) is False  # qualityCutoffNotMet True -> not met
    assert api.reference_date(ep, "airDateUtc") == "2025-01-01T00:00:00Z"
    assert api.reference_date(ep, "dateAdded") == "2026-02-02T00:00:00Z"


def test_sonarr_current_file_id_fallback():
    api = SonarrApi(_conn("sonarr"))
    assert api.current_file_id({"episodeFileId": 11}) == 11  # no episodeFile, fallback field
