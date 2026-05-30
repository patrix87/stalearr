"""Pure per-item decision: given fetched data, return ACT (with the release) or HOLD.

"Optimized" means the algorithm can no longer find anything better than the current file
(HOLD) — never merely "we triggered a grab". The swap rule is a single threshold: act iff
the pick's overall closeness beats the current file's by at least min_closeness_gain.
Closeness already weighs score, resolution, and size, so that one check covers both
shrinking a bloated file and a genuine quality upgrade.
"""

from dataclasses import dataclass

from optimizarr.features.optimizer.topsis import Topsis


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
) -> Decision:
    """Pure decision: given fetched data, return ACT (with the release) or HOLD.

    Swap iff the pick's overall closeness beats the current file's by at least
    min_closeness_gain — closeness already weighs score, resolution, and size."""
    pick, diag = topsis.pick(releases, runtime_h, profile_name, target_resolution)
    current_closeness, cur_raw = topsis.closeness_for_current_file(
        current_file or {}, runtime_h, profile_name, target_resolution
    )
    current = {"closeness": current_closeness, **cur_raw}

    if pick is None:
        return Decision(
            "HOLD",
            f"no viable candidate ({diag['inclusion']})",
            profile_name=profile_name,
            current=current,
            diag=diag,
        )

    release, attrs, pick_closeness = pick
    pick_info = {"closeness": pick_closeness, "title": release.get("title", "?"), **attrs["raw"]}
    gain = topsis.closeness_gain(pick_closeness, current_closeness)
    threshold = topsis.cfg.min_closeness_gain

    if gain >= threshold:
        action, reason = "ACT", f"closeness {gain:+.3f} >= {threshold}"
        out_release = release
    else:
        action, reason = "HOLD", f"closeness {gain:+.3f} < {threshold}"
        out_release = None

    return Decision(
        action,
        reason,
        profile_name=profile_name,
        current=current,
        pick=pick_info,
        release=out_release,
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
