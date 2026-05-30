from optimizarr.features.optimizer.config import default_topsis
from optimizarr.features.optimizer.topsis import GB, Topsis, eligible


def _topsis() -> Topsis:
    return Topsis(default_topsis())


def _release(score=900_000, resolution=2160, size_gb=10.0, rejections=None, temp=False):
    return {
        "customFormatScore": score,
        "quality": {"quality": {"resolution": resolution}},
        "size": int(size_gb * GB),
        "rejections": rejections or [],
        "temporarilyRejected": temp,
    }


def test_eligible_drops_blocklisted_and_temp():
    keep = eligible(
        [
            _release(),
            _release(rejections=["Release was blocklisted"]),
            _release(temp=True),
            _release(rejections=["Unable to parse release"]),
        ]
    )
    assert len(keep) == 1


def test_gbh_floor_drops_fake_2160p():
    t = _topsis()  # shared reference: 2160 floor 3.0 GiB/h
    fake = _release(resolution=2160, size_gb=4.0)  # 2.0 GiB/h < 3.0
    real = _release(resolution=2160, size_gb=20.0)  # 10 GiB/h
    kept = t.filter_by_gbh_floor([fake, real], 2.0)
    assert kept == [real]


def test_normalize_size_one_sided_plateau_then_ramp():
    t = _topsis()
    # aim=6.5, ceiling=18 (2160 Efficient-ish)
    assert t.normalize_size(3.0, 6.5, 18.0) == 1.0  # below aim: never penalized
    assert t.normalize_size(6.5, 6.5, 18.0) == 1.0  # at aim: still 1.0
    assert t.normalize_size(18.0, 6.5, 18.0) == 0.0  # at ceiling
    mid = t.normalize_size(12.25, 6.5, 18.0)  # halfway aim->ceiling
    assert abs(mid - 0.5) < 1e-9
    assert t.normalize_size(25.0, 6.5, 18.0) == 0.0  # past ceiling


def test_normalize_size_degenerate_ceiling_equals_aim():
    t = _topsis()
    assert t.normalize_size(6.5, 6.5, 6.5) == 1.0  # at/below aim
    assert t.normalize_size(7.0, 6.5, 6.5) == 0.0  # above a zero-width ramp


def test_score_gap_keeps_cluster_drops_tail_and_negatives():
    t = _topsis()  # default score_gap = 0.20
    rels = [
        _release(score=1_000_000),
        _release(score=950_000),
        _release(score=930_000),
        _release(score=200_000),  # 930k -> 200k is a >20% drop: the tail
        _release(score=-50),  # negatives always dropped
    ]
    kept = t.filter_by_score_gap(rels)
    scores = sorted((r["customFormatScore"] for r in kept), reverse=True)
    assert scores == [1_000_000, 950_000, 930_000]


def test_resolve_profile_matches_preset_by_name():
    t = _topsis()
    rp = t.resolve_profile("1080p Efficient")
    assert rp.weights == t.cfg.presets["Efficient"].weights
    assert rp.size_aim == t.cfg.presets["Efficient"].size_aim
    assert rp.pick == "topsis"


def test_resolve_profile_falls_back_to_default_preset():
    t = _topsis()
    rp = t.resolve_profile("Something Unmatched")
    assert rp.weights == t.cfg.presets[t.cfg.default_preset].weights


def test_resolve_profile_override_size_aim_and_pick():
    cfg = default_topsis()
    from optimizarr.features.optimizer.config import ProfileOverride

    cfg.profiles["Special"] = ProfileOverride(preset="Efficient", size_aim=0.4, pick="min_size")
    rp = Topsis(cfg).resolve_profile("Special")
    assert rp.size_aim == 0.4
    assert rp.pick == "min_size"
    assert rp.weights == cfg.presets["Efficient"].weights  # inherited


def test_score_candidates_orders_by_closeness():
    t = _topsis()
    rp = t.resolve_profile("2160p Quality")
    good = _release(score=1_000_000, resolution=2160, size_gb=13.0)  # 6.5 GiB/h at target
    weak = _release(score=950_000, resolution=1080, size_gb=14.0)  # lower res
    scored, diag = t.score_candidates([weak, good], 2.0, rp, target_resolution=2160)
    assert scored[0][0] is good
    assert scored[0][2] >= scored[1][2]
    assert diag["input"] == 2


def test_select_max_score_for_remux():
    t = _topsis()
    rp = t.resolve_profile("2160p Remux")
    big = _release(score=1_000_000, resolution=2160, size_gb=60.0)
    lean = _release(score=900_000, resolution=2160, size_gb=20.0)
    scored, _ = t.score_candidates([lean, big], 2.0, rp, 2160)
    selected = t.select(scored, rp)
    assert selected is not None
    rel, _attrs, _clo = selected
    assert rel is big  # highest score wins regardless of size


def test_select_min_size_for_compact():
    t = _topsis()
    rp = t.resolve_profile("Compact")
    small = _release(score=850_000, resolution=2160, size_gb=7.0)
    bigger = _release(score=1_000_000, resolution=2160, size_gb=13.0)
    scored, _ = t.score_candidates([bigger, small], 2.0, rp, 2160)
    selected = t.select(scored, rp)
    assert selected is not None
    rel, _attrs, _clo = selected
    assert rel is small  # smallest wins


def test_select_empty_returns_none():
    t = _topsis()
    assert t.select([], t.resolve_profile(None)) is None
