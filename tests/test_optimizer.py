from optimizarr.config import TopsisConfig
from optimizarr.optimizer import OptimizerWorker, decide
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


class _FakeAdapter:
    app = "radarr"

    def __init__(self, file_ids):
        self._file_ids = file_ids

    def current_file_id(self, item):
        return self._file_ids.get(item["id"])

    def item_id(self, item):
        return item["id"]


def _worker(tmp_path, state):
    cfg = type("C", (), {})()
    # Minimal config object good enough for reconciliation
    from optimizarr.config import OptimizerConfig

    cfg.optimizer = OptimizerConfig(enabled=True, apps=[])
    cfg.dry_run = False
    cfg.radarr = None
    cfg.sonarr = None
    w = OptimizerWorker.__new__(OptimizerWorker)
    w.opt = cfg.optimizer
    w.state = state
    return w


def test_reconcile_grab_succeeded(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    state.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=100)
    w = _worker(tmp_path, state)

    ctx = type("Ctx", (), {})()
    ctx.adapter = _FakeAdapter({1: 200})  # file id changed -> success
    ctx.items_by_id = {1: {"id": 1}}

    w._reconcile_in_flight(ctx, queue_ids=set())  # left the queue
    assert state.get("radarr", 1).status == SATISFIED
    assert state.get("radarr", 1).file_id == 200


def test_reconcile_grab_failed(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    state.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=100)
    w = _worker(tmp_path, state)

    ctx = type("Ctx", (), {})()
    ctx.adapter = _FakeAdapter({1: 100})  # file id unchanged -> failed
    ctx.items_by_id = {1: {"id": 1}}

    w._reconcile_in_flight(ctx, queue_ids=set())
    assert state.get("radarr", 1) is None  # cleared, will retry


def test_reconcile_still_in_queue_untouched(tmp_path):
    state = StateManager(str(tmp_path / "s.json"))
    state.mark_in_flight("radarr", 1, guid="g", file_id_at_grab=100)
    w = _worker(tmp_path, state)

    ctx = type("Ctx", (), {})()
    ctx.adapter = _FakeAdapter({1: 100})
    ctx.items_by_id = {1: {"id": 1}}

    w._reconcile_in_flight(ctx, queue_ids={1})  # still downloading
    assert state.get("radarr", 1).status == IN_FLIGHT
