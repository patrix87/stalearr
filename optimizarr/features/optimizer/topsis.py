"""TOPSIS-based release picker.

Multi-objective release selection. Three axes, each normalized to [0,1]:
  - score:      Profilarr customFormatScore, fixed scale [anti_ideal, ideal] (higher better)
  - resolution: pixel height toward the profile target (higher better, low weight — Profilarr
                already folds resolution into score, so this axis mostly avoids double-counting)
  - size:       GB/h on a cost curve — smaller is better, 1.0 at the per-preset floor, 0 at bloat

Inclusion (before scoring): drop hard rejections, drop below the per-preset gb/h floor (fakes),
then gap-cut the score tail (keep the top cluster down to the first relative drop > score_gap).

A profile's weights and size curve come from a named preset (Remux/Quality/Balanced/Efficient/
Compact, or a per-profile override), resolved from the profile name. All tuning is config-driven.
"""

from __future__ import annotations

import math

from optimizarr.features.optimizer.config import TopsisConfig

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

    # ----- profile -> preset resolution -----

    def _match_preset(self, profile_name: str) -> str:
        """First preset whose name is a case-insensitive substring of the profile name;
        preset definition order breaks ties (so Remux wins over Quality in 'Remux Quality')."""
        low = profile_name.lower()
        for name in self.cfg.presets:
            if name.lower() in low:
                return name
        return self.cfg.default_preset

    def resolve_profile(
        self, profile_name: str | None
    ) -> tuple[dict[str, float], dict[int, tuple[float, float, float]]]:
        """Return (weights, size_by_resolution) for a profile, honoring exact-name overrides
        then name-keyword preset matching then default_preset."""
        cfg = self.cfg
        override = cfg.profiles.get(profile_name) if profile_name else None
        if override and override.preset:
            base = cfg.presets[override.preset]
        elif profile_name:
            base = cfg.presets[self._match_preset(profile_name)]
        else:
            base = cfg.presets[cfg.default_preset]
        weights = override.weights if (override and override.weights) else base.weights
        size = (
            override.size_by_resolution
            if (override and override.size_by_resolution)
            else base.size_by_resolution
        )
        return weights, size

    def _size_for(
        self, size_curve: dict[int, tuple[float, float, float]], res: int
    ) -> tuple[float, float, float]:
        if res in size_curve:
            return size_curve[res]
        keys = sorted(size_curve)
        if not keys:
            return (0.0, 0.0, float("inf"))
        below = [k for k in keys if k <= res]
        return size_curve[below[-1]] if below else size_curve[keys[0]]

    # ----- pre-filters -----

    def filter_by_gbh_floor(
        self,
        releases: list[dict],
        runtime_h: float,
        size_curve: dict[int, tuple[float, float, float]],
    ) -> list[dict]:
        """Drop releases whose GB/h is below the per-preset/per-resolution floor — catches
        fakes/upscales and "wrong kind of release for this preset" sizes."""
        keep = []
        for r in releases:
            floor, _target, _bloat = self._size_for(size_curve, _release_resolution(r))
            if _release_gbh(r, runtime_h) >= floor:
                keep.append(r)
        return keep

    def filter_by_score_gap(self, releases: list[dict]) -> list[dict]:
        """Keep the top score cluster: sort desc, scan high->low, cut at the first consecutive
        relative drop greater than score_gap. Negatives are always dropped."""
        nonneg = [r for r in releases if (r.get("customFormatScore") or 0) >= 0]
        if not nonneg:
            return []
        srt = sorted(nonneg, key=lambda r: -(r.get("customFormatScore") or 0))
        kept = [srt[0]]
        for prev, cur in zip(srt, srt[1:], strict=False):
            ps = prev.get("customFormatScore") or 0
            cs = cur.get("customFormatScore") or 0
            if ps > 0 and (ps - cs) / ps > self.cfg.score_gap:
                break
            kept.append(cur)
        return kept

    def apply_prefilters(
        self,
        releases: list[dict],
        runtime_h: float,
        size_curve: dict[int, tuple[float, float, float]],
    ) -> tuple[list[dict], dict]:
        """Run all pre-filters in order; return (kept, diag) with per-stage counts."""
        diag: dict[str, object] = {"input": len(releases)}
        after_hard = eligible(releases)
        diag["after_hard_rejections"] = len(after_hard)
        after_gbh = self.filter_by_gbh_floor(after_hard, runtime_h, size_curve)
        diag["after_gbh_floor"] = len(after_gbh)
        kept = self.filter_by_score_gap(after_gbh)
        diag["after_score_gap"] = len(kept)
        diag["inclusion"] = f"gap-cut (>{self.cfg.score_gap:.0%} drop)"
        return kept, diag

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

    def normalize_size(self, gbh: float, floor: float, target: float, bloat: float) -> float:
        """Tent: linear floor -> target (rising to 1.0), linear target -> bloat (falling to 0).
        When target == floor the tent degenerates to a pure cost curve (smaller wins): n_size
        is 1.0 at the floor and falls linearly to 0 at bloat."""
        if gbh < floor or gbh >= bloat or bloat <= floor:
            return 0.0
        if target <= floor:
            return (bloat - gbh) / (bloat - floor)  # cost-only
        if gbh <= target:
            return (gbh - floor) / (target - floor)
        return (bloat - gbh) / (bloat - target)

    def attributes_for(
        self,
        release: dict,
        runtime_h: float,
        size_curve: dict[int, tuple[float, float, float]],
        target_resolution: int | None = None,
    ) -> dict:
        """Normalized [0,1] attributes + raw values for one release."""
        size_bytes = release.get("size", 0)
        gbh = _release_gbh(release, runtime_h)
        res = _release_resolution(release)
        score = release.get("customFormatScore", 0)
        floor, target, bloat = self._size_for(size_curve, res)
        return {
            "n_score": self.normalize_score(score),
            "n_resolution": self.normalize_resolution(res, target_resolution),
            "n_size": self.normalize_size(gbh, floor, target, bloat),
            "raw": {
                "score": score,
                "resolution": res,
                "gbh": gbh,
                "size_gb": size_bytes / GB,
                "envelope": (floor, target, bloat),
            },
        }

    def closeness(self, attrs: dict, weights: dict[str, float]) -> float:
        """TOPSIS closeness in [0,1]. 1 = ideal, 0 = anti-ideal."""
        w = {
            "n_score": weights["score"],
            "n_resolution": weights["resolution"],
            "n_size": weights["size"],
        }
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
        weights, size_curve = self.resolve_profile(profile_name)
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
        floor, target, bloat = self._size_for(size_curve, res)
        attrs = {
            "n_score": self.normalize_score(score),
            "n_resolution": self.normalize_resolution(res, target_resolution),
            "n_size": self.normalize_size(gbh, floor, target, bloat),
        }
        return self.closeness(attrs, weights), raw

    # ----- ranking -----

    def rank(
        self,
        releases: list[dict],
        runtime_h: float,
        profile_name: str | None = None,
        target_resolution: int | None = None,
    ) -> tuple[list[tuple[dict, dict, float]], dict]:
        """Return (ranked: [(release, attrs, closeness)], prefilter_diag)."""
        weights, size_curve = self.resolve_profile(profile_name)
        kept, diag = self.apply_prefilters(releases, runtime_h, size_curve)
        scored = [
            (r, a, self.closeness(a, weights))
            for r, a in (
                (r, self.attributes_for(r, runtime_h, size_curve, target_resolution)) for r in kept
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
    ) -> tuple[tuple[dict, dict, float] | None, dict]:
        """Return ((release, attrs, closeness), diag), or (None, diag)."""
        ranked, diag = self.rank(releases, runtime_h, profile_name, target_resolution)
        return (ranked[0] if ranked else None), diag

    # ----- swap decision -----

    def closeness_gain(self, pick_closeness: float, current_closeness: float | None) -> float:
        """How much closeness the pick adds over the current file. An unknown current
        closeness (no score on the current file) is treated as the worst case (0.0)."""
        baseline = current_closeness if current_closeness is not None else 0.0
        return pick_closeness - baseline

    def should_swap(self, pick_closeness: float, current_closeness: float | None) -> bool:
        """Swap iff the pick improves overall closeness by at least min_closeness_gain."""
        return self.closeness_gain(pick_closeness, current_closeness) >= self.cfg.min_closeness_gain
