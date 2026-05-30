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
    t = _topsis()
    _w, size_curve = t.resolve_profile(None)  # Balanced: 2160 floor 1.5 GB/h
    fake = _release(resolution=2160, size_gb=2.0)  # 1.0 GB/h < 1.5
    real = _release(resolution=2160, size_gb=20.0)  # 10 GB/h
    kept = t.filter_by_gbh_floor([fake, real], 2.0, size_curve)
    assert kept == [real]


def test_normalize_size_tent_peaks_at_target():
    t = _topsis()
    # floor=2, target=8, bloat=20 — tent
    assert t.normalize_size(2.0, 2.0, 8.0, 20.0) == 0.0  # at floor: rising slope starts at 0
    assert t.normalize_size(5.0, 2.0, 8.0, 20.0) == 0.5  # halfway up to target
    assert t.normalize_size(8.0, 2.0, 8.0, 20.0) == 1.0  # at target: peak
    assert t.normalize_size(14.0, 2.0, 8.0, 20.0) == 0.5  # halfway target -> bloat
    assert t.normalize_size(20.0, 2.0, 8.0, 20.0) == 0.0  # at bloat
    assert t.normalize_size(1.0, 2.0, 8.0, 20.0) == 0.0  # below floor
    assert t.normalize_size(25.0, 2.0, 8.0, 20.0) == 0.0  # above bloat


def test_normalize_size_cost_when_target_equals_floor():
    t = _topsis()
    # target == floor -> Compact-style cost curve: smallest wins
    assert t.normalize_size(2.0, 2.0, 2.0, 10.0) == 1.0  # at floor
    assert t.normalize_size(6.0, 2.0, 2.0, 10.0) == 0.5  # halfway down
    assert t.normalize_size(10.0, 2.0, 2.0, 10.0) == 0.0  # at bloat


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
    weights, _size = t.resolve_profile("1080p Efficient")
    assert weights == t.cfg.presets["Efficient"].weights


def test_resolve_profile_falls_back_to_default_preset():
    t = _topsis()
    weights, _size = t.resolve_profile("Something Unmatched")
    assert weights == t.cfg.presets[t.cfg.default_preset].weights


def test_rank_orders_by_closeness_and_pick_is_top():
    t = _topsis()
    # Both sized at their Quality preset target (2160 target=16 GB/h; 1080 target=7 GB/h).
    good = _release(score=1_000_000, resolution=2160, size_gb=32.0)  # 16 GB/h, max axes
    weak = _release(score=950_000, resolution=1080, size_gb=14.0)  # 7 GB/h, lower res+score
    ranked, diag = t.rank([weak, good], runtime_h=2.0, profile_name="2160p Quality")
    assert ranked[0][0] is good
    assert ranked[0][2] >= ranked[1][2]
    assert diag["input"] == 2


def test_should_swap_when_closeness_gain_meets_threshold():
    t = _topsis()  # default min_closeness_gain = 0.02
    assert t.should_swap(pick_closeness=0.991, current_closeness=0.944)


def test_should_not_swap_when_gain_below_threshold():
    t = _topsis()
    assert not t.should_swap(pick_closeness=0.951, current_closeness=0.944)  # +0.007 < 0.02


def test_should_not_swap_on_quality_regression():
    t = _topsis()
    assert not t.should_swap(pick_closeness=0.90, current_closeness=0.95)  # negative gain


def test_should_swap_treats_unknown_current_as_worst():
    t = _topsis()
    assert t.should_swap(pick_closeness=0.5, current_closeness=None)
