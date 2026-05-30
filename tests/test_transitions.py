import random

from optimizarr.features.optimizer.config import default_topsis
from optimizarr.features.optimizer.topsis import Topsis
from optimizarr.features.optimizer.transitions import classify, is_forbidden, matrix

CFG = default_topsis()
T = Topsis(CFG)


def _legal(profile: str, **kw) -> bool:
    rp = T.resolve_profile(profile)
    d = classify(t=rp.transitions, **kw)
    forbidden, _ = is_forbidden(d, rp.transitions)
    return not forbidden


def _attrs(score, gbh, res):
    return {
        "cand_nscore": T.normalize_score(score),
        "cand_gbh": gbh,
        "cand_res": res,
        "cand_score": score,
    }


# ----- universal rules -----


def test_bigger_same_res_no_score_gain_forbidden_for_all():
    # current: 2160p, 6 GiB/h, score 800k. candidate: same res+score but bigger.
    for profile in ("2160p Remux", "2160p Quality", "2160p Balanced", "2160p Efficient", "Compact"):
        assert not _legal(
            profile,
            cur_nscore=T.normalize_score(800_000),
            cur_gbh=6.0,
            cur_res=2160,
            **_attrs(800_000, 10.0, 2160),
        ), profile


def test_smaller_much_lower_score_forbidden_for_size_leaners():
    # Efficient: a much smaller file but a big score drop is still forbidden.
    assert not _legal(
        "2160p Efficient",
        cur_nscore=T.normalize_score(900_000),
        cur_gbh=8.0,
        cur_res=2160,
        **_attrs(500_000, 3.0, 2160),  # -0.40 n_score = much lower
    )


def test_lower_score_bigger_same_res_forbidden():
    assert not _legal(
        "2160p Quality",
        cur_nscore=T.normalize_score(900_000),
        cur_gbh=6.0,
        cur_res=2160,
        **_attrs(850_000, 12.0, 2160),
    )


def test_smaller_at_equal_score_is_a_free_win_for_all():
    for profile in ("2160p Remux", "2160p Quality", "2160p Balanced", "2160p Efficient", "Compact"):
        assert _legal(
            profile,
            cur_nscore=T.normalize_score(800_000),
            cur_gbh=8.0,
            cur_res=2160,
            **_attrs(800_000, 5.0, 2160),  # same score, ~37% smaller
        ), profile


# ----- resolution axis -----


def test_resolution_downgrade_always_forbidden():
    assert not _legal(
        "2160p Efficient",
        cur_nscore=T.normalize_score(800_000),
        cur_gbh=6.0,
        cur_res=2160,
        **_attrs(900_000, 2.0, 1080),  # higher score, smaller, but lower res
    )


def test_resolution_upgrade_allowed_even_if_bigger():
    # 1080p current -> 2160p candidate, bigger and same score: a legitimate res upgrade.
    assert _legal(
        "2160p Efficient",
        cur_nscore=T.normalize_score(800_000),
        cur_gbh=2.0,
        cur_res=1080,
        **_attrs(800_000, 6.0, 2160),
    )


def test_resolution_upgrade_blocked_when_much_lower_score():
    assert not _legal(
        "2160p Efficient",
        cur_nscore=T.normalize_score(900_000),
        cur_gbh=2.0,
        cur_res=1080,
        **_attrs(400_000, 6.0, 2160),
    )


# ----- per-profile distinctions -----


def test_remux_refuses_any_score_drop():
    # slightly lower score, much smaller file: Efficient takes it, Remux does not.
    kw = dict(
        cur_nscore=T.normalize_score(900_000),
        cur_gbh=8.0,
        cur_res=2160,
        **_attrs(850_000, 3.0, 2160),
    )
    assert _legal("2160p Efficient", **kw)
    assert not _legal("2160p Remux", **kw)


def test_quality_slight_drop_needs_much_smaller_balanced_does_not():
    # slightly lower score, only modestly smaller (~15%).
    kw = dict(
        cur_nscore=T.normalize_score(900_000),
        cur_gbh=6.0,
        cur_res=2160,
        **_attrs(850_000, 5.0, 2160),
    )
    assert _legal("2160p Balanced", **kw)
    assert not _legal("2160p Quality", **kw)


def test_compact_never_takes_a_bigger_file_even_for_higher_score():
    assert not _legal(
        "Compact",
        cur_nscore=T.normalize_score(700_000),
        cur_gbh=3.0,
        cur_res=2160,
        **_attrs(1_000_000, 6.0, 2160),  # much higher score but bigger
    )


def test_compact_accepts_much_lower_score_only_if_much_smaller():
    base = dict(cur_nscore=T.normalize_score(900_000), cur_gbh=6.0, cur_res=2160)
    assert _legal("Compact", **base, **_attrs(500_000, 2.5, 2160))  # much lower + much smaller
    assert not _legal(
        "Compact", **base, **_attrs(500_000, 5.5, 2160)
    )  # much lower, only ~8% smaller


def test_quality_bigger_allowed_only_with_much_higher_score():
    base = dict(cur_nscore=T.normalize_score(800_000), cur_gbh=6.0, cur_res=2160)
    assert not _legal("2160p Quality", **base, **_attrs(830_000, 9.0, 2160))  # +0.03 only
    assert _legal("2160p Quality", **base, **_attrs(950_000, 9.0, 2160))  # +0.15 clears score_much


# ----- matrix shape sanity (pins the spec) -----


def test_matrix_cells_match_spec():
    m = matrix(CFG.presets["Remux"].transitions)
    assert m[("same", "smaller")] is True
    assert m[("same", "same")] is False
    assert m[("slightly_lower", "much_smaller")] is False  # Remux: no drops

    q = matrix(CFG.presets["Quality"].transitions)
    assert q[("slightly_lower", "much_smaller")] is True
    assert q[("slightly_lower", "smaller")] is False  # needs *much* smaller

    c = matrix(CFG.presets["Compact"].transitions)
    assert all(
        c[(row, "bigger")] is False
        for row in ("much_higher", "higher", "same", "slightly_lower", "much_lower")
    )


# ----- the headline guarantee: no oscillation -----


def test_no_two_file_oscillation_on_real_presets():
    """For every shipped preset and random file pairs, A->B and B->A are never both legal."""
    rnd = random.Random(20260530)
    for profile in ("Remux", "Quality", "Balanced", "Efficient", "Compact"):
        rp = T.resolve_profile(profile)
        for _ in range(5000):
            a = (
                rnd.randint(0, 1_000_000),
                round(rnd.uniform(0.5, 40), 2),
                rnd.choice([720, 1080, 2160]),
            )
            b = (
                rnd.randint(0, 1_000_000),
                round(rnd.uniform(0.5, 40), 2),
                rnd.choice([720, 1080, 2160]),
            )
            na, nb = T.normalize_score(a[0]), T.normalize_score(b[0])
            dab = classify(
                cur_nscore=na,
                cand_nscore=nb,
                cur_gbh=a[1],
                cand_gbh=b[1],
                cur_res=a[2],
                cand_res=b[2],
                cand_score=b[0],
                t=rp.transitions,
            )
            dba = classify(
                cur_nscore=nb,
                cand_nscore=na,
                cur_gbh=b[1],
                cand_gbh=a[1],
                cur_res=b[2],
                cand_res=a[2],
                cand_score=a[0],
                t=rp.transitions,
            )
            fab, _ = is_forbidden(dab, rp.transitions)
            fba, _ = is_forbidden(dba, rp.transitions)
            assert fab or fba, (profile, a, b)
