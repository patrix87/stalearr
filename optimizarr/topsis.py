"""TOPSIS-based release picker.

Multi-objective release selection (Technique for Order of Preference by Similarity
to Ideal Solution). Three benefit axes, each normalized to [0,1]:
  - score:      Profilarr customFormatScore (higher = better)
  - resolution: pixel height (higher = better, capped at the profile target)
  - size:       GB/h, scored by an asymmetric tent (peaks at target, 0 below and at bloat)

Two pre-filters run before TOPSIS: a GB/h sanity floor (drop fake/low-bitrate encodes)
and an adaptive score floor (four tiers; negatives always dropped). closeness =
d(anti-ideal) / (d(ideal) + d(anti-ideal)); pick the highest.

All tuning comes from a TopsisConfig (see optimizarr.config); this module holds no
tunable globals so behavior is fully driven by config.toml.
"""

import math

from optimizarr.config import TopsisConfig

GB = 1024**3

# Rejections meaning "can't be considered at all"; everything else is advisory.
HARD_REJECT_KEYWORDS = (
    "blocklisted",
    "Unable to parse",
    "Unknown Movie",
    "Not enough seeders",
)


def _release_gbh(release: dict, runtime_h: float) -> float:
    if not runtime_h or runtime_h <= 0:
        return 0.0
    return (release.get("size", 0) / GB) / runtime_h


def _release_resolution(release: dict) -> int:
    return ((release.get("quality") or {}).get("quality") or {}).get("resolution") or 0


def eligible(releases: list[dict]) -> list[dict]:
    """Drop hard-rejected releases (blocklist, parse failure, dead torrents)."""
    keep = []
    for r in releases:
        if r.get("temporarilyRejected"):
            continue
        rejections = r.get("rejections") or []
        if any(any(k in reason for k in HARD_REJECT_KEYWORDS) for reason in rejections):
            continue
        keep.append(r)
    return keep


class Topsis:
    """Config-driven TOPSIS picker. One instance per optimizer run."""

    def __init__(self, cfg: TopsisConfig):
        self.cfg = cfg

    # ----- weights & envelopes -----

    def weights_for(self, profile_name: str | None) -> dict[str, float]:
        by_profile = self.cfg.weights_by_profile.get(profile_name) if profile_name else None
        return by_profile or self.cfg.weights

    def envelope_for_release(
        self, profile_name: str | None, release_resolution: int
    ) -> tuple[float, float]:
        """(target_gbh, bloat_gbh). Profile-specific envelope when the release matches
        the profile's target resolution; per-resolution defaults otherwise."""
        profile_env = (
            self.cfg.size_envelope_by_profile.get(profile_name) if profile_name else None
        ) or {}
        if release_resolution in profile_env:
            return profile_env[release_resolution]
        return self.cfg.size_envelope_by_resolution.get(release_resolution, (3.0, 25.0))

    # ----- pre-filters -----

    def filter_by_gbh_floor(self, releases: list[dict], runtime_h: float) -> list[dict]:
        """Drop releases whose GB/h is below the per-resolution sanity floor — catches
        obvious fakes (e.g. 1 GB/h '2160p') that would still get nonzero n_size."""
        keep = []
        for r in releases:
            floor = self.cfg.sanity_gbh_floor_by_resolution.get(_release_resolution(r))
            if floor is None or _release_gbh(r, runtime_h) >= floor:
                keep.append(r)
        return keep

    def filter_by_score_floor(
        self, releases: list[dict], current_file_score: int | None
    ) -> tuple[list[dict], str]:
        """Adaptive score floor across four tiers. Returns (kept, tier_label).

        Tier 1: >= score_floor_preferred. Tier 2: >= current file's score (no downgrade).
        Tier 2.5: >= top - score_drop_from_top. Tier 3: >= 0. Negatives always dropped.
        """
        cfg = self.cfg

        def s(r: dict) -> int:
            return r.get("customFormatScore") or 0

        non_negative = [r for r in releases if s(r) >= 0]

        tier1 = [r for r in non_negative if s(r) >= cfg.score_floor_preferred]
        if tier1:
            return tier1, f"tier1 (>= {cfg.score_floor_preferred})"

        if current_file_score is not None and current_file_score >= 0:
            tier2 = [r for r in non_negative if s(r) >= current_file_score]
            if tier2:
                return tier2, f"tier2 (>= current file's {current_file_score})"

        if non_negative:
            top = max(s(r) for r in non_negative)
            floor = top - cfg.score_drop_from_top
            tier25 = [r for r in non_negative if s(r) >= floor]
            if tier25:
                return tier25, f"tier2.5 (>= top {top} - {cfg.score_drop_from_top} = {floor})"

        if non_negative:
            return non_negative, "tier3 (>= 0)"
        return [], "empty (all < 0)"

    def apply_prefilters(
        self, releases: list[dict], runtime_h: float, current_file_score: int | None
    ) -> tuple[list[dict], dict]:
        """Run all pre-filters in order; return (kept, diag) with per-stage counts."""
        diag: dict[str, object] = {"input": len(releases)}
        after_hard = eligible(releases)
        diag["after_hard_rejections"] = len(after_hard)
        after_gbh = self.filter_by_gbh_floor(after_hard, runtime_h)
        diag["after_gbh_floor"] = len(after_gbh)
        after_score, tier = self.filter_by_score_floor(after_gbh, current_file_score)
        diag["after_score_floor"] = len(after_score)
        diag["score_floor_tier"] = tier
        return after_score, diag

    # ----- normalization -----

    def normalize_score(self, s: float) -> float:
        cfg = self.cfg
        if s >= cfg.score_ideal:
            return 1.0
        if s <= cfg.score_anti_ideal:
            return 0.0
        return (s - cfg.score_anti_ideal) / (cfg.score_ideal - cfg.score_anti_ideal)

    def normalize_resolution(self, r: int, target: int | None = None) -> float:
        cfg = self.cfg
        ideal = target if target else cfg.resolution_ideal
        if r >= ideal:
            return 1.0
        if r <= cfg.resolution_anti_ideal:
            return 0.0
        return (r - cfg.resolution_anti_ideal) / (ideal - cfg.resolution_anti_ideal)

    def normalize_size(self, gbh: float, target: float, bloat: float) -> float:
        """Asymmetric tent. 1.0 at target; linear to 0 at gbh=0 and at bloat."""
        if gbh <= 0 or bloat <= target:
            return 0.0
        if gbh < target:
            return gbh / target
        if gbh >= bloat:
            return 0.0
        return 1.0 - (gbh - target) / (bloat - target)

    def attributes_for(
        self,
        release: dict,
        runtime_h: float,
        profile_name: str | None = None,
        target_resolution: int | None = None,
    ) -> dict:
        """Normalized [0,1] attributes + raw values for one release."""
        size_bytes = release.get("size", 0)
        gbh = _release_gbh(release, runtime_h)
        res = _release_resolution(release)
        score = release.get("customFormatScore", 0)
        target_gbh, bloat_gbh = self.envelope_for_release(profile_name, res)
        return {
            "n_score": self.normalize_score(score),
            "n_resolution": self.normalize_resolution(res, target_resolution),
            "n_size": self.normalize_size(gbh, target_gbh, bloat_gbh),
            "raw": {
                "score": score,
                "resolution": res,
                "gbh": gbh,
                "size_gb": size_bytes / GB,
                "envelope": (target_gbh, bloat_gbh),
            },
        }

    def closeness(self, attrs: dict, profile_name: str | None = None) -> float:
        """TOPSIS closeness in [0,1]. 1 = ideal, 0 = anti-ideal."""
        wd = self.weights_for(profile_name)
        w = {"n_score": wd["score"], "n_resolution": wd["resolution"], "n_size": wd["size"]}
        d_ideal = math.sqrt(sum(w[k] * (1.0 - attrs[k]) ** 2 for k in w))
        d_anti = math.sqrt(sum(w[k] * attrs[k] ** 2 for k in w))
        total = d_ideal + d_anti
        return 0.0 if total == 0 else d_anti / total

    def closeness_for_current_file(
        self,
        movie_file: dict,
        runtime_h: float,
        profile_name: str | None = None,
        target_resolution: int | None = None,
    ) -> tuple[float | None, dict]:
        """Closeness for the existing library file (None if its score is unknown)."""
        size = movie_file.get("size", 0) or 0
        size_gb = size / GB
        gbh = (size_gb / runtime_h) if (runtime_h and runtime_h > 0) else 0.0
        score = movie_file.get("customFormatScore")
        mi = movie_file.get("mediaInfo") or {}
        res = 0
        res_str = mi.get("resolution") or ""
        if "x" in res_str:
            try:
                res = int(res_str.split("x")[1])
            except (IndexError, ValueError):
                res = 0
        raw = {"score": score, "resolution": res, "gbh": gbh, "size_gb": size_gb}
        if score is None:
            return None, raw
        target_gbh, bloat_gbh = self.envelope_for_release(profile_name, res)
        attrs = {
            "n_score": self.normalize_score(score),
            "n_resolution": self.normalize_resolution(res, target_resolution),
            "n_size": self.normalize_size(gbh, target_gbh, bloat_gbh),
        }
        return self.closeness(attrs, profile_name), raw

    # ----- ranking -----

    def rank(
        self,
        releases: list[dict],
        runtime_h: float,
        profile_name: str | None = None,
        target_resolution: int | None = None,
        current_file_score: int | None = None,
    ) -> tuple[list[tuple[dict, dict, float]], dict]:
        """Return (ranked: [(release, attrs, closeness)], prefilter_diag)."""
        kept, diag = self.apply_prefilters(releases, runtime_h, current_file_score)
        scored = [
            (r, a, self.closeness(a, profile_name))
            for r, a in (
                (r, self.attributes_for(r, runtime_h, profile_name, target_resolution))
                for r in kept
            )
        ]
        scored.sort(key=lambda x: -x[2])
        return scored, diag

    def pick(
        self,
        releases: list[dict],
        runtime_h: float,
        profile_name: str | None = None,
        target_resolution: int | None = None,
        current_file_score: int | None = None,
    ) -> tuple[tuple[dict, dict, float] | None, dict]:
        """Return ((release, attrs, closeness), diag), or (None, diag)."""
        ranked, diag = self.rank(
            releases, runtime_h, profile_name, target_resolution, current_file_score
        )
        return (ranked[0] if ranked else None), diag

    # ----- gates (swap decision) -----

    def evaluate_gates(
        self,
        pick_closeness: float,
        pick_size_bytes: int,
        current_closeness: float | None,
        current_size_bytes: int,
    ) -> list[tuple[str, bool | None, str]]:
        """Evaluate swap gates across two paths to ACT.
        Returns [(name, passed, detail)] with dotted path_a.* / path_b.* names."""
        cfg = self.cfg
        gates: list[tuple[str, bool | None, str]] = []
        savings_gb = (current_size_bytes - pick_size_bytes) / GB

        if current_closeness is None:
            gates.append(("path_a.closeness", None, "current closeness unknown"))
            passed = savings_gb >= cfg.min_size_savings_gb
            sign = ">=" if passed else "<"
            detail = f"{savings_gb:+.2f} GB {sign} {cfg.min_size_savings_gb}"
            gates.append(("path_a.size_savings", passed, detail))
            return gates

        gain = pick_closeness - current_closeness

        a_close = gain >= cfg.min_closeness_gain
        detail = f"d {gain:+.3f} {'>=' if a_close else '<'} {cfg.min_closeness_gain}"
        gates.append(("path_a.closeness", a_close, detail))

        a_size = savings_gb >= cfg.min_size_savings_gb
        detail = f"{savings_gb:+.2f} GB {'>=' if a_size else '<'} {cfg.min_size_savings_gb}"
        gates.append(("path_a.size_savings", a_size, detail))

        if cfg.allow_upgrades:
            min_up = cfg.min_closeness_gain_for_upgrade
            b_close = gain >= min_up
            detail = f"d {gain:+.3f} {'>=' if b_close else '<'} {min_up}"
            gates.append(("path_b.closeness", b_close, detail))

        return gates

    def should_swap(
        self,
        pick_closeness: float,
        current_closeness: float | None,
        pick_size_bytes: int,
        current_size_bytes: int,
    ) -> bool:
        gates = self.evaluate_gates(
            pick_closeness, pick_size_bytes, current_closeness, current_size_bytes
        )
        return all_gates_pass(gates)


def winning_path(gates: list[tuple[str, bool | None, str]]) -> str | None:
    by_path: dict[str, list[bool | None]] = {}
    for name, passed, _ in gates:
        if "." not in name:
            continue
        by_path.setdefault(name.split(".", 1)[0], []).append(passed)
    for path in ("path_a", "path_b"):
        gs = by_path.get(path) or []
        if gs and all(g is True for g in gs):
            return path
    return None


def all_gates_pass(gates: list[tuple[str, bool | None, str]]) -> bool:
    return winning_path(gates) is not None


def max_allowed_resolution(profile_items: list[dict]) -> int:
    """Max `resolution` over allowed entries in a Radarr/Sonarr quality profile's
    items[] (with nested items[]). Returns 0 if nothing is allowed."""
    best = 0
    for it in profile_items or []:
        q = it.get("quality") or {}
        if it.get("allowed") and q.get("resolution"):
            best = max(best, q["resolution"])
        for sub in it.get("items") or []:
            sq = sub.get("quality") or {}
            if sub.get("allowed") and sq.get("resolution"):
                best = max(best, sq["resolution"])
    return best
