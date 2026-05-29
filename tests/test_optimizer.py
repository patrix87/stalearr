from datetime import UTC, datetime

from optimizarr.config import Connection, OptimizerAppConfig, OptimizerConfig, TopsisConfig
from optimizarr.optimizer import (
    ArrOptimizer,
    OptimizerWorker,
    RadarrOptimizer,
    _AppContext,
    decide,
    format_decision,
)
from optimizarr.state import IN_FLIGHT, SATISFIED, StateManager
from optimizarr.topsis import GB, Topsis


def _topsis() -> Topsis:
    return Topsis(TopsisConfig())


def _release(guid="g1", score=1_000_000, resolution=2160, size_gb=14.0):
    return {
        "guid": guid,
        "indexerId": 1,
        "title": f"Movie.{resolution}p",
        "customFormatScore": score,
        "quality": {"quality": {"resolution": resolution}},
        "size": int(size_gb * GB),
        "rejections": [],
    }


def _file(score=200_000, resolution="1920x1080", size_gb=30.0):
    return {
        "id": 555,
        "customFormatScore": score,
        "size": int(size_gb * GB),
        "mediaInfo": {"resolution": resolution},
    }


NOW = datetime(2026, 5, 28, tzinfo=UTC)


def _radarr_adapter(min_age_days, release_type="digitalRelease"):
    return RadarrOptimizer(
        Connection(name="radarr", url="http://x", api_key="k"),
        OptimizerAppConfig(min_age_days=min_age_days, release_type=release_type),
    )


def test_age_gate_disabled_passes_everything():
    a = _radarr_adapter(min_age_days=0)
    assert a.age_ok({"digitalRelease": "2026-05-27T00:00:00Z"}, NOW)  # 1 day old, still ok
    assert a.age_ok({}, NOW)  # no date, still ok when gate off


def test_age_gate_blocks_recent_and_allows_old():
    a = _radarr_adapter(min_age_days=14)
    assert not a.age_ok({"digitalRelease": "2026-05-20T00:00:00Z"}, NOW)  # 8 days < 14
    assert a.age_ok({"digitalRelease": "2026-01-01T00:00:00Z"}, NOW)  # old enough
    assert not a.age_ok({}, NOW)  # unknown date is skipped when gating is on


def test_age_gate_date_added_reads_movie_file():
    a = _radarr_adapter(min_age_days=14, release_type="dateAdded")
    item = {
        "digitalRelease": "2026-05-27T00:00:00Z",
        "movieFile": {"dateAdded": "2026-01-01T00:00:00Z"},
    }
    assert a.age_ok(item, NOW)  # uses movieFile.dateAdded, which is old


def test_format_decision_act_shows_current_and_pick():
    releases = [_release(score=1_000_000, resolution=2160, size_gb=14.0)]
    d = decide(
        _topsis(),
        releases,
        runtime_h=2.0,
        profile_name="2160p Quality",
        target_resolution=2160,
        current_file=_file(score=200_000, resolution="1920x1080"),
    )
    msg = format_decision("radarr", "Movie (2024)", d, dry_run=True)
    assert "would GRAB" in msg
    assert "current:" in msg and "pick:" in msg
    assert "profile=2160p Quality" in msg
    assert "Δsize" in msg and "Δcloseness" in msg


def test_format_decision_hold_shows_failing_gates():
    # Marginal candidate -> HOLD, with gate detail explaining why.
    releases = [_release(score=1_000_000, resolution=2160, size_gb=14.0)]
    current = _file(score=1_000_000, resolution="3840x2160", size_gb=14.0)
    d = decide(
        _topsis(),
        releases,
        runtime_h=2.0,
        profile_name="2160p Quality",
        target_resolution=2160,
        current_file=current,
    )
    assert d.action == "HOLD"
    msg = format_decision("radarr", "Movie (2024)", d, dry_run=False)
    assert "HOLD" in msg
    assert "gates not met:" in msg


def test_decide_hold_when_no_candidates():
    d = decide(
        _topsis(),
        [],
        runtime_h=2.0,
        profile_name=None,
        target_resolution=None,
        current_file=_file(),
    )
    assert d.action == "HOLD"


def test_decide_act_on_clear_upgrade():
    # Current is a bloated 1080p low-score file; candidate is a clean 2160p high-score.
    releases = [_release(score=1_000_000, resolution=2160, size_gb=14.0)]
    d = decide(
        _topsis(),
        releases,
        runtime_h=2.0,
        profile_name="2160p Quality",
        target_resolution=2160,
        current_file=_file(score=200_000, resolution="1920x1080"),
    )
    assert d.action == "ACT"
    assert d.release is not None
    assert d.release["guid"] == "g1"


def test_decide_hold_when_current_already_good():
    # Current file is already excellent and small; nothing better.
    releases = [_release(score=1_000_000, resolution=2160, size_gb=14.0)]
    current = _file(score=1_000_000, resolution="3840x2160", size_gb=14.0)
    d = decide(
        _topsis(),
        releases,
        runtime_h=2.0,
        profile_name="2160p Quality",
        target_resolution=2160,
        current_file=current,
    )
    assert d.action == "HOLD"


# ----- reconciliation -----


class _FakeAdapter(ArrOptimizer):
    app = "radarr"

    def __init__(self, file_ids):
        self._file_ids = file_ids

    def current_file_id(self, item):
        return self._file_ids.get(item["id"])

    def item_id(self, item):
        return item["id"]


def _worker(state):
    w = OptimizerWorker.__new__(OptimizerWorker)
    w.opt = OptimizerConfig(enabled=True, apps=[])
    w.state = state
    return w


def _ctx(file_ids):
    ctx = _AppContext(_FakeAdapter(file_ids))
    ctx.items_by_id = {1: {"id": 1}}
    return ctx


def test_reconcile_grab_succeeded(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    state.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=100)
    w = _worker(state)

    w._reconcile_in_flight(_ctx({1: 200}), queue_ids=set())  # file id changed -> success
    entry = state.get("radarr", 1)
    assert entry is not None
    assert entry.status == SATISFIED
    assert entry.file_id == 200


def test_reconcile_grab_failed(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    state.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=100)
    w = _worker(state)

    w._reconcile_in_flight(_ctx({1: 100}), queue_ids=set())  # file id unchanged -> failed
    assert state.get("radarr", 1) is None  # cleared, will retry


def test_reconcile_still_in_queue_untouched(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    state.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=100)
    w = _worker(state)

    w._reconcile_in_flight(_ctx({1: 100}), queue_ids={1})  # still downloading
    entry = state.get("radarr", 1)
    assert entry is not None
    assert entry.status == IN_FLIGHT
