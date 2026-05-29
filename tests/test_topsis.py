from optimizarr.features.optimizer.config import TopsisConfig
from optimizarr.features.optimizer.topsis import (
    GB,
    Topsis,
    eligible,
)


def _topsis() -> Topsis:
    return Topsis(TopsisConfig())


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
    # 2160p floor is 1.5 GB/h; a 2h movie at 2 GB = 1.0 GB/h is below floor.
    fake = _release(resolution=2160, size_gb=2.0)
    real = _release(resolution=2160, size_gb=20.0)
    kept = t.filter_by_gbh_floor([fake, real], runtime_h=2.0)
    assert kept == [real]


def test_score_floor_tiers():
    t = _topsis()
    # Tier 1: something >= 900k
    kept, tier = t.filter_by_score_floor([_release(score=950_000), _release(score=100)], None)
    assert tier.startswith("tier1")
    assert len(kept) == 1

    # No 900k and no current-file score: tier2.5 catches the non-negatives, negatives dropped
    kept, tier = t.filter_by_score_floor([_release(score=50), _release(score=-100)], None)
    assert tier.startswith("tier2.5")
    assert all((r.get("customFormatScore") or 0) >= 0 for r in kept)

    # Tier 2: nothing >= 900k but a candidate meets the current file's score
    kept, tier = t.filter_by_score_floor(
        [_release(score=500_000), _release(score=100_000)], current_file_score=400_000
    )
    assert tier.startswith("tier2")
    assert len(kept) == 1

    # All negative -> empty
    kept, tier = t.filter_by_score_floor([_release(score=-5)], None)
    assert kept == []
    assert tier.startswith("empty")


def test_normalize_size_asymmetric_tent():
    t = _topsis()
    assert t.normalize_size(6.0, 6.0, 25.0) == 1.0  # at target
    assert t.normalize_size(3.0, 6.0, 25.0) == 0.5  # halfway below target
    assert t.normalize_size(25.0, 6.0, 25.0) == 0.0  # at bloat
    assert t.normalize_size(0.0, 6.0, 25.0) == 0.0  # zero


def test_rank_orders_by_closeness_and_pick_is_top():
    t = _topsis()
    good = _release(score=1_000_000, resolution=2160, size_gb=14.0)
    weak = _release(score=900_000, resolution=1080, size_gb=14.0)
    ranked, diag = t.rank([weak, good], runtime_h=2.0, profile_name="2160p Quality")
    assert ranked[0][0] is good
    assert ranked[0][2] >= ranked[1][2]
    assert diag["input"] == 2


def test_should_swap_when_closeness_gain_meets_threshold():
    t = _topsis()  # default min_closeness_gain = 0.02
    # The Marvels case: small-but-real closeness gain from a smaller, higher-score file.
    assert t.should_swap(pick_closeness=0.991, current_closeness=0.944)


def test_should_not_swap_when_gain_below_threshold():
    t = _topsis()
    assert not t.should_swap(pick_closeness=0.951, current_closeness=0.944)  # +0.007 < 0.02


def test_should_not_swap_on_quality_regression():
    t = _topsis()
    assert not t.should_swap(pick_closeness=0.90, current_closeness=0.95)  # negative gain


def test_should_swap_treats_unknown_current_as_worst():
    t = _topsis()
    # No score on the current file -> baseline 0.0 -> any decent pick clears the bar.
    assert t.should_swap(pick_closeness=0.5, current_closeness=None)
