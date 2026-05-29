from optimizarr.features.optimizer.config import TopsisConfig
from optimizarr.features.optimizer.decision import decide, format_decision
from optimizarr.features.optimizer.topsis import GB, Topsis


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


def test_format_decision_hold_shows_closeness_reason():
    # Current file already excellent -> marginal gain -> HOLD, reason shows the delta.
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
    assert "closeness" in msg and "<" in msg  # "closeness +0.000 < 0.02"


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
