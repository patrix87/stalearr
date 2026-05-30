"""TOPSIS-based release scorer + per-profile pickers.

Multi-objective release scoring. Three axes, each normalized to [0,1]:
  - score:      Profilarr customFormatScore, fixed scale [anti_ideal, ideal] (higher better)
  - resolution: pixel height toward the profile target (higher better, low weight — Profilarr
                already folds resolution into score, so this axis mostly avoids double-counting)
  - size:       GiB/h on a ONE-SIDED curve — n_size = 1.0 at/below the profile's aim
                (`size_aim * reference.target`), ramping to 0 at the reference ceiling. Smaller
                than the aim is never penalized, so nothing is ever inflated to "reach" a target.

Inclusion (before scoring): drop hard rejections, drop below the shared reference gb/h floor
(fakes), then gap-cut the score tail (keep the top cluster down to the first relative drop >
score_gap).

The size reference `{floor, target, ceiling}` is shared by all profiles (config-driven); a
profile only contributes its weights, a relative `size_aim`, a `pick` method, and its transition
rules. The legality gate lives in transitions.py; this module scores and picks among survivors.
"""

from __future__ import annotations

import math

from optimizarr.features.optimizer.config import ResolvedProfile, TopsisConfig

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
    """Config-driven scorer + picker. One instance per optimizer run."""

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

    def resolve_profile(self, profile_name: str | None) -> ResolvedProfile:
        """Resolve a profile name to weights + size_aim + pick + transitions, honoring an
        exact-name override, then name-keyword preset matching, then default_preset."""
        cfg = self.cfg
        override = cfg.profiles.get(profile_name) if profile_name else None
        if override and override.preset:
            base = cfg.presets[override.preset]
        elif profile_name:
            base = cfg.presets[self._match_preset(profile_name)]
        else:
            base = cfg.presets[cfg.default_preset]
        weights = override.weights if (override and override.weights) else base.weights
        size_aim = (
            override.size_aim if (override and override.size_aim is not None) else base.size_aim
        )
        pick = override.pick if (override and override.pick) else base.pick
        return ResolvedProfile(
            weights=weights, size_aim=size_aim, pick=pick, transitions=base.transitions
        )

    def reference_for(self, res: int) -> tuple[float, float, float]:
        """Shared (floor, target, ceiling) for a resolution; nearest-defined-at-or-below."""
        ref = self.cfg.reference
        if res in ref:
            return ref[res]
        keys = sorted(ref)
        below = [k for k in keys if k <= res]
        return ref[below[-1]] if below else ref[keys[0]]

    # ----- pre-filters -----

    def filter_by_gbh_floor(self, releases: list[dict], runtime_h: float) -> list[dict]:
        """Drop releases whose GiB/h is below the shared per-resolution floor — catches
        fakes/upscales and other too-soft-for-the-resolution encodes."""
        keep = []
        for r in releases:
            floor, _target, _ceiling = self.reference_for(_release_resolution(r))
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

    def apply_prefilters(self, releases: list[dict], runtime_h: float) -> tuple[list[dict], dict]:
        """Run all pre-filters in order; return (kept, diag) with per-stage counts."""
        diag: dict[str, object] = {"input": len(releases)}
        after_hard = eligible(releases)
        diag["after_hard_rejections"] = len(after_hard)
        after_gbh = self.filter_by_gbh_floor(after_hard, runtime_h)
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

    def normalize_size(self, gbh: float, aim: float, ceiling: float) -> float:
        """One-sided cost curve: 1.0 at or below `aim`, linear down to 0 at `ceiling`. Smaller
        than the aim is never penalized — that is what keeps the optimizer from ever inflating a
        file to reach a target."""
        if gbh <= aim:
            return 1.0
        if gbh >= ceiling or ceiling <= aim:
            return 0.0
        return (ceiling - gbh) / (ceiling - aim)

    def _attrs(
        self,
        score: float,
        res: int,
        gbh: float,
        size_gb: float,
        resolved: ResolvedProfile,
        target_resolution: int | None,
    ) -> dict:
        floor, target, ceiling = self.reference_for(res)
        aim = resolved.size_aim * target
        return {
            "n_score": self.normalize_score(score or 0),
            "n_resolution": self.normalize_resolution(res, target_resolution),
            "n_size": self.normalize_size(gbh, aim, ceiling),
            "raw": {
                "score": score,
                "resolution": res,
                "gbh": gbh,
                "size_gb": size_gb,
                "reference": (floor, target, ceiling),
                "aim": aim,
            },
        }

    def attributes_for(
        self,
        release: dict,
        runtime_h: float,
        resolved: ResolvedProfile,
        target_resolution: int | None = None,
    ) -> dict:
        """Normalized [0,1] attributes + raw values for one release."""
        size_bytes = release.get("size", 0)
        return self._attrs(
            release.get("customFormatScore", 0),
            _release_resolution(release),
            _release_gbh(release, runtime_h),
            size_bytes / GB,
            resolved,
            target_resolution,
        )

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

    def _current_resolution(self, movie_file: dict) -> int:
        mi = movie_file.get("mediaInfo") or {}
        res_str = mi.get("resolution") or ""
        if "x" in res_str:
            try:
                return int(res_str.split("x")[1])
            except (IndexError, ValueError):
                return 0
        return 0

    def current_attributes(
        self,
        movie_file: dict,
        runtime_h: float,
        resolved: ResolvedProfile,
        target_resolution: int | None = None,
    ) -> dict | None:
        """Normalized attributes for the existing library file (None if its score is unknown)."""
        score = movie_file.get("customFormatScore")
        if score is None:
            return None
        size = movie_file.get("size", 0) or 0
        size_gb = size / GB
        gbh = (size_gb / runtime_h) if (runtime_h and runtime_h > 0) else 0.0
        res = self._current_resolution(movie_file)
        return self._attrs(score, res, gbh, size_gb, resolved, target_resolution)

    def closeness_for_current_file(
        self,
        movie_file: dict,
        runtime_h: float,
        resolved: ResolvedProfile,
        target_resolution: int | None = None,
    ) -> tuple[float | None, dict]:
        """Closeness for the existing library file (None if its score is unknown)."""
        attrs = self.current_attributes(movie_file, runtime_h, resolved, target_resolution)
        if attrs is None:
            size = movie_file.get("size", 0) or 0
            size_gb = size / GB
            gbh = (size_gb / runtime_h) if (runtime_h and runtime_h > 0) else 0.0
            return None, {
                "score": None,
                "resolution": self._current_resolution(movie_file),
                "gbh": gbh,
                "size_gb": size_gb,
            }
        return self.closeness(attrs, resolved.weights), attrs["raw"]

    # ----- scoring & picking -----

    def score_candidates(
        self,
        releases: list[dict],
        runtime_h: float,
        resolved: ResolvedProfile,
        target_resolution: int | None = None,
    ) -> tuple[list[tuple[dict, dict, float]], dict]:
        """Pre-filter, then return (scored: [(release, attrs, closeness)], diag), sorted best
        first by closeness (with deterministic tie-breaks)."""
        kept, diag = self.apply_prefilters(releases, runtime_h)
        scored = [
            (r, a, self.closeness(a, resolved.weights))
            for r, a in (
                (r, self.attributes_for(r, runtime_h, resolved, target_resolution)) for r in kept
            )
        ]
        scored.sort(key=lambda x: (-x[2], -(x[1]["raw"]["score"] or 0), x[1]["raw"]["gbh"]))
        return scored, diag

    def select(
        self, candidates: list[tuple[dict, dict, float]], resolved: ResolvedProfile
    ) -> tuple[dict, dict, float] | None:
        """Choose one candidate by the profile's pick method. `candidates` are assumed already
        gated (every entry is a legal transition); ties break deterministically."""
        if not candidates:
            return None
        if resolved.pick == "max_score":
            return max(candidates, key=lambda x: (x[1]["raw"]["score"] or 0, -x[1]["raw"]["gbh"]))
        if resolved.pick == "min_size":
            return min(candidates, key=lambda x: (x[1]["raw"]["gbh"], -(x[1]["raw"]["score"] or 0)))
        # topsis: already sorted best-first by score_candidates
        return max(candidates, key=lambda x: x[2])
