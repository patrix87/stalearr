"""Pure per-item decision: given fetched data, return ACT (with the release) or HOLD.

"Optimized" means the algorithm can no longer find anything better than the current file
(HOLD) — never merely "we triggered a grab". The decision is two steps:

  1. **Transition gate** (transitions.py): drop every candidate whose move from the current file
     is forbidden for this profile. A surviving candidate is, by construction, a legal and
     beneficial change — there is no separate "is it worth it" threshold.
  2. **Pick** (topsis.py): choose among the survivors by the profile's pick method (TOPSIS for
     the multi-axis profiles, max_score for Remux, min_size for Compact).

ACT iff at least one candidate survives the gate; otherwise HOLD (and the worker marks the item
satisfied). Closeness is still computed for the human-readable log lines.
"""

from dataclasses import dataclass

from optimizarr.features.optimizer.topsis import Topsis
from optimizarr.features.optimizer.transitions import classify, is_forbidden


@dataclass
class Decision:
    action: str  # "ACT" or "HOLD"
    reason: str
    profile_name: str | None = None
    current: dict | None = None  # {score, resolution, gbh, size_gb, closeness}
    pick: dict | None = None  # {score, resolution, gbh, size_gb, closeness, title}
    release: dict | None = None  # raw release to grab (ACT only)
    diag: dict | None = None


def decide(
    topsis: Topsis,
    releases: list[dict],
    runtime_h: float,
    profile_name: str | None,
    target_resolution: int | None,
    current_file: dict | None,
    allow_size_increase: bool = True,
    allow_quality_downgrade: bool = True,
) -> Decision:
    """Pure decision: gate the candidates against the current file, then pick the best survivor.

    Two optional pre-filters apply before scoring (per-app policy):
      - allow_size_increase=False drops releases bigger than the current file;
      - allow_quality_downgrade=False drops releases with a lower customFormatScore."""
    cur = current_file or {}
    cur_size = cur.get("size")
    if not allow_size_increase and isinstance(cur_size, int) and cur_size > 0:
        releases = [r for r in releases if r.get("size", 0) <= cur_size]
    cur_score = cur.get("customFormatScore")
    if not allow_quality_downgrade and cur_score is not None:
        releases = [r for r in releases if (r.get("customFormatScore") or 0) >= cur_score]

    resolved = topsis.resolve_profile(profile_name)
    scored, diag = topsis.score_candidates(releases, runtime_h, resolved, target_resolution)

    cur_attrs = topsis.current_attributes(cur, runtime_h, resolved, target_resolution)
    current_closeness, cur_raw = topsis.closeness_for_current_file(
        cur, runtime_h, resolved, target_resolution
    )
    current = {"closeness": current_closeness, **cur_raw}

    # Gate baseline: an unknown current score is treated as the worst case (n_score 0), so any
    # scored candidate reads as an improvement.
    cur_nscore = cur_attrs["n_score"] if cur_attrs is not None else 0.0
    cur_gbh = cur_raw.get("gbh", 0.0) or 0.0
    cur_res = cur_raw.get("resolution", 0) or 0

    legal: list[tuple[dict, dict, float]] = []
    for rel, attrs, clo in scored:
        deltas = classify(
            cur_nscore=cur_nscore,
            cand_nscore=attrs["n_score"],
            cur_gbh=cur_gbh,
            cand_gbh=attrs["raw"]["gbh"],
            cur_res=cur_res,
            cand_res=attrs["raw"]["resolution"],
            cand_score=int(attrs["raw"]["score"] or 0),
            t=resolved.transitions,
        )
        forbidden, _reason = is_forbidden(deltas, resolved.transitions)
        if not forbidden:
            legal.append((rel, attrs, clo))
    diag["after_gate"] = len(legal)

    if not legal:
        why = f"no viable candidate ({diag['inclusion']})" if not scored else "nothing better"
        return Decision("HOLD", why, profile_name=profile_name, current=current, diag=diag)

    selected = topsis.select(legal, resolved)
    assert selected is not None  # legal is non-empty (guarded above); select is None only on []
    release, attrs, pick_closeness = selected
    pick_info = {"closeness": pick_closeness, "title": release.get("title", "?"), **attrs["raw"]}
    return Decision(
        "ACT",
        f"legal {resolved.pick} pick of {len(legal)}",
        profile_name=profile_name,
        current=current,
        pick=pick_info,
        release=release,
        diag=diag,
    )


def _fmt_side(side: dict | None) -> str:
    if not side:
        return "n/a"
    score = side.get("score")
    score_s = f"{score:,}" if score is not None else "n/a"
    clo = side.get("closeness")
    clo_s = f"{clo:.3f}" if clo is not None else "n/a"
    res = side.get("resolution") or 0
    res_s = f"{res}p" if res else "?"
    return (
        f"score={score_s} res={res_s} size={side.get('size_gb', 0):.1f}GB "
        f"({side.get('gbh', 0):.1f} GB/h) closeness={clo_s}"
    )


def _fmt_deltas(current: dict | None, pick: dict | None) -> str:
    if not current or not pick:
        return ""
    parts = []
    c_clo, p_clo = current.get("closeness"), pick.get("closeness")
    if c_clo is not None and p_clo is not None:
        parts.append(f"Δcloseness {p_clo - c_clo:+.3f}")
    parts.append(f"Δsize {pick.get('size_gb', 0) - current.get('size_gb', 0):+.1f}GB")
    return "  (" + ", ".join(parts) + ")" if parts else ""


def format_decision(app: str, label: str, decision: Decision, dry_run: bool) -> str:
    """Multi-line, human-readable explanation of one decision (current vs pick)."""
    profile = decision.profile_name or "?"
    if decision.action == "ACT":
        verb = "would GRAB" if dry_run else "GRAB"
        head = f"[{app}] {verb} — {label}  [profile={profile}]  ({decision.reason})"
    else:
        head = f"[{app}] HOLD — {label}  [profile={profile}]  ({decision.reason})"

    lines = [head, f"    current: {_fmt_side(decision.current)}"]
    if decision.pick:
        candidate_label = "pick" if decision.action == "ACT" else "best   "
        lines.append(
            f"    {candidate_label}: {_fmt_side(decision.pick)}"
            f"{_fmt_deltas(decision.current, decision.pick)}"
        )
        lines.append(f"    release: {decision.pick.get('title', '?')}")
    return "\n".join(lines)
