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
from optimizarr.state import SATISFIED, StateManager
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


# ----- _process_one: grab vs HOLD, and what gets persisted -----


class _ProcessAdapter(ArrOptimizer):
    """Adapter double serving canned data to _process_one and recording grabs."""

    app = "radarr"

    def __init__(self, releases, current_file):
        self._releases = releases
        self._current = current_file
        self.grabbed: list[dict] = []

    def runtime_h(self, item):
        return 2.0

    def profile_for(self, item):
        return ("2160p Quality", 2160)

    def current_file(self, item):
        return self._current

    def current_file_id(self, item):
        return (self._current or {}).get("id")

    def releases(self, item):
        return self._releases

    def label(self, item):
        return "Movie (2024)"

    def grab(self, release):
        self.grabbed.append(release)


def _worker(state, dry_run=False):
    w = OptimizerWorker.__new__(OptimizerWorker)
    w.opt = OptimizerConfig(enabled=True, apps=[])
    w.state = state
    w.topsis = Topsis(TopsisConfig())
    w.dry_run = dry_run
    return w


def _ctx(adapter):
    ctx = _AppContext(adapter)
    ctx.items_by_id = {1: {"id": 1}}
    return ctx


def test_process_one_hold_marks_satisfied(tmp_path):
    # Current file is already excellent; the candidate is no better -> HOLD -> satisfied.
    state = StateManager(str(tmp_path / "s.json"))
    adapter = _ProcessAdapter(
        releases=[_release(score=1_000_000, resolution=2160, size_gb=14.0)],
        current_file=_file(score=1_000_000, resolution="3840x2160", size_gb=14.0),
    )
    _worker(state)._process_one(_ctx(adapter), 1)
    entry = state.get("radarr", 1)
    assert entry is not None and entry.status == SATISFIED
    assert adapter.grabbed == []


def test_process_one_act_grabs_without_marking(tmp_path):
    # A clear upgrade is grabbed, but the item is NOT marked satisfied — it stays in the
    # pool until a later evaluation HOLDs (success) or it's retried (failure).
    state = StateManager(str(tmp_path / "s.json"))
    adapter = _ProcessAdapter(
        releases=[_release(score=1_000_000, resolution=2160, size_gb=14.0)],
        current_file=_file(score=200_000, resolution="1920x1080", size_gb=30.0),
    )
    _worker(state)._process_one(_ctx(adapter), 1)
    assert len(adapter.grabbed) == 1
    assert state.get("radarr", 1) is None


def test_process_one_dry_run_does_not_grab(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    adapter = _ProcessAdapter(
        releases=[_release(score=1_000_000, resolution=2160, size_gb=14.0)],
        current_file=_file(score=200_000, resolution="1920x1080", size_gb=30.0),
    )
    _worker(state, dry_run=True)._process_one(_ctx(adapter), 1)
    assert adapter.grabbed == []
    assert state.get("radarr", 1) is None


def test_build_pool_holds_progress_across_refresh_then_resets(tmp_path):
    # A list refresh must NOT restart the pass: items already evaluated stay excluded
    # until the whole active set is covered, then the pass resets.
    state = StateManager(str(tmp_path / "s.json"))
    w = _worker(state)
    ctx = _AppContext(_ProcessAdapter([], None))
    ctx.items_by_id = {1: {"id": 1}, 2: {"id": 2}, 3: {"id": 3}}
    now = datetime.now(UTC)

    w._build_pool(ctx, now)
    assert set(ctx.pool) == {1, 2, 3}

    # Two items processed this pass; a refresh happened (evaluated preserved).
    ctx.evaluated = {1, 2}
    w._build_pool(ctx, now)
    assert ctx.pool == [3]  # only the unvisited item remains

    # Last item visited -> pool empties -> pass resets to a fresh full sweep.
    ctx.evaluated = {1, 2, 3}
    w._build_pool(ctx, now)
    assert set(ctx.pool) == {1, 2, 3}
    assert ctx.evaluated == set()


def test_build_pool_excludes_satisfied(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    state.mark_satisfied("radarr", 2)
    w = _worker(state)
    ctx = _AppContext(_ProcessAdapter([], None))
    ctx.items_by_id = {1: {"id": 1}, 2: {"id": 2}}
    w._build_pool(ctx, datetime.now(UTC))
    assert ctx.pool == [1]  # satisfied item 2 is out of the pool
