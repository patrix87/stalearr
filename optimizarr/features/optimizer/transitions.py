"""The transition gate: which moves from the current file to a candidate release are *legal*.

This replaces the old single closeness-gain swap test. Legality is a set of hard constraints,
some universal and some per-profile; TOPSIS (or a deterministic pick) only ever chooses among
candidates that survive this gate. See docs/condition-matrix-design.md for the rationale and the
per-profile matrices.

Two properties matter:
  - **Space-saving:** a bigger file is only ever legal as the side effect of a real
    score/resolution gain — never on its own.
  - **No oscillation:** the accept relation is a strict partial order. With fixed thresholds and
    `score_much > score_slack` / `size_much > size_slack` (enforced at config load), if A->B is
    an accepted improvement then B->A is forbidden, so two files can't ping-pong.

Axis classification (all relative to the current file):
  - score: on Δn_score (normalized), bucketed much_higher/higher/same/slightly_lower/much_lower
  - size:  on the relative GiB/h delta, bucketed bigger/same/smaller/much_smaller
  - resolution: candidate height vs current height (and vs the profile target, handled by caller)
"""

from __future__ import annotations

from dataclasses import dataclass

from optimizarr.features.optimizer.config import Transitions

# Score buckets, worst -> best.
MUCH_LOWER = "much_lower"
SLIGHTLY_LOWER = "slightly_lower"
SAME = "same"
HIGHER = "higher"
MUCH_HIGHER = "much_higher"

# Size buckets.
MUCH_SMALLER = "much_smaller"
SMALLER = "smaller"
SIZE_SAME = "same"
BIGGER = "bigger"

# Resolution comparison.
RES_LOWER = "lower"
RES_SAME = "same"
RES_HIGHER = "higher"


@dataclass
class Deltas:
    score: str  # one of the score buckets
    size: str  # one of the size buckets
    resolution: str  # candidate resolution vs current: lower / same / higher
    cand_score: int  # raw candidate score (for the viability check)

    @property
    def score_is_higher(self) -> bool:
        return self.score in (HIGHER, MUCH_HIGHER)

    @property
    def size_is_smaller(self) -> bool:
        return self.size in (SMALLER, MUCH_SMALLER)


def _score_bucket(delta: float, t: Transitions) -> str:
    if delta > t.score_much:
        return MUCH_HIGHER
    if delta > t.score_slack:
        return HIGHER
    if delta >= -t.score_slack:
        return SAME
    if delta >= -t.score_much:
        return SLIGHTLY_LOWER
    return MUCH_LOWER


def _size_bucket(rel: float, t: Transitions) -> str:
    if rel < -t.size_much:
        return MUCH_SMALLER
    if rel < -t.size_slack:
        return SMALLER
    if rel <= t.size_slack:
        return SIZE_SAME
    return BIGGER


def classify(
    *,
    cur_nscore: float,
    cand_nscore: float,
    cur_gbh: float,
    cand_gbh: float,
    cur_res: int,
    cand_res: int,
    cand_score: int,
    t: Transitions,
) -> Deltas:
    """Bucket the three axis deltas between the current file and one candidate."""
    score = _score_bucket(cand_nscore - cur_nscore, t)
    # No current size info (unknown runtime / empty file) -> treat as "same size" so the decision
    # falls through to the score/resolution axes.
    rel = (cand_gbh - cur_gbh) / cur_gbh if cur_gbh > 0 else 0.0
    size = _size_bucket(rel, t)
    if cand_res > cur_res:
        resolution = RES_HIGHER
    elif cand_res < cur_res:
        resolution = RES_LOWER
    else:
        resolution = RES_SAME
    return Deltas(score=score, size=size, resolution=resolution, cand_score=cand_score)


def is_forbidden(d: Deltas, t: Transitions) -> tuple[bool, str]:
    """Return (forbidden, reason). `forbidden=False` means this move is a legal candidate.

    Order matters: resolution changes are handled first (an upgrade waives the size rules; a
    downgrade is never allowed), then the score x size matrix applies at equal resolution."""
    # ----- resolution axis -----
    if d.resolution == RES_LOWER:
        return True, "resolution downgrade"
    if d.resolution == RES_HIGHER:
        # A resolution upgrade toward the target: the larger size is expected, so size rules are
        # waived. We still refuse to drop a lot of score for it.
        if d.score == MUCH_LOWER:
            return True, "resolution upgrade but much-lower score"
        return False, "resolution upgrade"

    # ----- same resolution: score x size matrix -----
    # Universal rule: a bigger file with no score increase is never acceptable.
    if d.size == BIGGER and not d.score_is_higher:
        return True, "bigger file without a score increase"

    if d.score_is_higher:
        if d.size == BIGGER:
            if not t.allow_bigger_for_score:
                return True, "profile refuses a bigger file"
            if t.bigger_needs_much_score and d.score != MUCH_HIGHER:
                return True, "bigger file needs a large score gain"
            return False, "higher score, bigger file"
        return False, "higher score"  # smaller or same size + higher score

    if d.score == SAME:
        if d.size_is_smaller:
            return False, "same score, smaller file"
        return True, "no improvement"  # same/same (current) or same score + same size

    if d.score == SLIGHTLY_LOWER:
        if not t.accept_score_drop:
            return True, "profile refuses any score drop"
        if not d.size_is_smaller:
            return True, "slightly-lower score without a smaller file"
        if t.slight_drop_needs_much_smaller and d.size != MUCH_SMALLER:
            return True, "slightly-lower score needs a much smaller file"
        if d.cand_score < t.viability_score:
            return True, "below viability score"
        return False, "slightly-lower score, smaller file"

    # MUCH_LOWER
    if not t.accept_much_lower_score:
        return True, "much-lower score"
    if d.size != MUCH_SMALLER:
        return True, "much-lower score without a much smaller file"
    if d.cand_score < t.viability_score:
        return True, "below viability score"
    return False, "much-lower score, much smaller file"


# ----- documentation / test helper -----

_SCORE_ROWS = (MUCH_HIGHER, HIGHER, SAME, SLIGHTLY_LOWER, MUCH_LOWER)
_SIZE_COLS = (MUCH_SMALLER, SMALLER, SIZE_SAME, BIGGER)


def matrix(t: Transitions, *, viable: bool = True) -> dict[tuple[str, str], bool]:
    """Render the at-equal-resolution score x size legality matrix for a profile: True = legal.
    `viable` controls whether the candidate's raw score clears viability_score (only affects the
    score-drop rows). Used by tests to pin each cell and to regenerate the doc tables."""
    cand_score = t.viability_score if viable else t.viability_score - 1
    out: dict[tuple[str, str], bool] = {}
    for srow in _SCORE_ROWS:
        for scol in _SIZE_COLS:
            d = Deltas(score=srow, size=scol, resolution=RES_SAME, cand_score=cand_score)
            forbidden, _reason = is_forbidden(d, t)
            out[(srow, scol)] = not forbidden
    return out
