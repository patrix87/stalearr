"""Optimizer feature configuration: schema + parsing of the [optimizer] TOML section.

Tuning values (presets, size curves, anchors) come from the merged config (defaults.toml +
the user's config.toml) — there are no magic defaults baked into this module. The shared
loader (optimizarr.config) delegates to parse_optimizer() here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from optimizarr.config import RADARR_RELEASE_TYPES, SONARR_RELEASE_TYPES

PICK_ORDERS = {"random", "ordered"}


@dataclass
class Preset:
    """A named bundle: TOPSIS weights + a per-resolution size tent {floor, target, bloat}."""

    weights: dict[str, float]  # keys: score, resolution, size (sum 1.0)
    # resolution -> (floor, target, bloat) GB/h. target == floor degenerates to a cost curve.
    size_by_resolution: dict[int, tuple[float, float, float]]


@dataclass
class ProfileOverride:
    """Exact-name override: reference a preset, or give explicit weights / size curve."""

    preset: str | None = None
    weights: dict[str, float] | None = None
    size_by_resolution: dict[int, tuple[float, float, float]] | None = None


@dataclass
class TopsisConfig:
    score_ideal: int
    score_anti_ideal: int
    resolution_ideal: int
    resolution_anti_ideal: int
    score_gap: float
    min_closeness_gain: float
    default_preset: str
    presets: dict[str, Preset]
    profiles: dict[str, ProfileOverride] = field(default_factory=dict)


@dataclass
class OptimizerAppConfig:
    enabled: bool = True
    min_age_days: int = 0
    # List of date fields the age gate checks. ALL listed dates must be at least
    # min_age_days old. Two-gate default ([release, dateAdded]) avoids touching freshly
    # released items (still being chased by Radarr/Sonarr) and freshly imported files.
    release_type: list[str] = field(default_factory=list)
    # If False, releases bigger than the current file are filtered out before scoring —
    # blocks resolution upgrades too (1080p -> 2160p is always a size increase).
    allow_size_increase: bool = True
    # If False, releases with a lower score than the current file are filtered out before
    # scoring. NOTE: turning this off neutralizes size-leaning presets (Compact/Efficient),
    # which are designed to swap a slightly-lower-score release for a meaningfully smaller one.
    allow_quality_downgrade: bool = True
    # If True, queue items waiting for manual import don't count toward queue_max — only
    # actively downloading/queued items do. Keeps the optimizer flowing when downgrades or
    # other rejected items pile up in the import-pending state.
    ignore_completed_in_queue: bool = True
    # If True, on each tick the worker scans the queue for completed items rejected solely
    # for score regression ("Not an upgrade") and force-imports them through manualimport.
    # Other rejection categories (executable/sample/mismatch) are left untouched.
    auto_import_downgrades: bool = True


@dataclass
class OptimizerConfig:
    enabled: bool = False
    queue_max: int = 5
    pick_order: str = "random"
    process_interval_seconds: int = 15
    list_refresh_minutes: int = 15
    reevaluate_after_days: int = 30
    radarr: OptimizerAppConfig = field(default_factory=OptimizerAppConfig)
    sonarr: OptimizerAppConfig = field(default_factory=OptimizerAppConfig)
    topsis: TopsisConfig = field(default_factory=lambda: default_topsis())


# ----- parsing helpers -----


def _weights(raw: dict, where: str) -> dict[str, float]:
    w = {k: float(raw[k]) for k in ("score", "resolution", "size") if k in raw}
    missing = {"score", "resolution", "size"} - w.keys()
    if missing:
        raise ValueError(f"{where}: missing weight keys {sorted(missing)}")
    total = sum(w.values())
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"{where}: weights must sum to 1.0, got {total:.3f}")
    return w


def _size_curve(raw: dict, where: str) -> dict[int, tuple[float, float, float]]:
    out: dict[int, tuple[float, float, float]] = {}
    for res, entry in raw.items():
        try:
            res_int = int(res)
        except (TypeError, ValueError) as e:
            raise ValueError(f"{where}: key {res!r} is not an integer resolution") from e
        if not isinstance(entry, dict) or not {"floor", "target", "bloat"} <= entry.keys():
            raise ValueError(f"{where}.{res}: expected {{floor, target, bloat}}, got {entry!r}")
        floor = float(entry["floor"])
        target = float(entry["target"])
        bloat = float(entry["bloat"])
        if bloat <= floor:
            raise ValueError(f"{where}.{res}: bloat ({bloat}) must exceed floor ({floor})")
        if not (floor <= target <= bloat):
            raise ValueError(
                f"{where}.{res}: target ({target}) must satisfy floor <= target <= bloat"
            )
        out[res_int] = (floor, target, bloat)
    return out


def _parse_preset(raw: dict, where: str) -> Preset:
    return Preset(
        weights=_weights(raw, where),
        size_by_resolution=_size_curve(raw.get("size_by_resolution", {}), f"{where}.size"),
    )


def _parse_profile_override(raw: dict, where: str) -> ProfileOverride:
    weights = _weights(raw["weights"], f"{where}.weights") if "weights" in raw else None
    size = (
        _size_curve(raw["size_by_resolution"], f"{where}.size_by_resolution")
        if "size_by_resolution" in raw
        else None
    )
    return ProfileOverride(preset=raw.get("preset"), weights=weights, size_by_resolution=size)


def _parse_topsis(raw: dict) -> TopsisConfig:
    presets = {
        name: _parse_preset(p, f"presets.{name}") for name, p in raw.get("presets", {}).items()
    }
    if not presets:
        raise ValueError("optimizer.topsis.presets is empty (defaults.toml should define them)")
    default_preset = str(raw.get("default_preset", "Balanced"))
    if default_preset not in presets:
        raise ValueError(f"default_preset {default_preset!r} is not a defined preset")
    profiles = {
        name: _parse_profile_override(o, f"profiles.{name}")
        for name, o in raw.get("profiles", {}).items()
    }
    for name, ov in profiles.items():
        if ov.preset is not None and ov.preset not in presets:
            raise ValueError(f"profiles.{name}.preset {ov.preset!r} is not a defined preset")
    return TopsisConfig(
        score_ideal=int(raw["score_ideal"]),
        score_anti_ideal=int(raw["score_anti_ideal"]),
        resolution_ideal=int(raw["resolution_ideal"]),
        resolution_anti_ideal=int(raw["resolution_anti_ideal"]),
        score_gap=float(raw["score_gap"]),
        min_closeness_gain=float(raw["min_closeness_gain"]),
        default_preset=default_preset,
        presets=presets,
        profiles=profiles,
    )


def _parse_release_types(raw: object, allowed: set[str], where: str) -> list[str]:
    if isinstance(raw, str):
        raise ValueError(
            f"{where}.release_type must be a list of strings, got string {raw!r}. "
            f'Use release_type = ["{raw}"]'
        )
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{where}.release_type must be a non-empty list, got {raw!r}")
    out: list[str] = []
    for entry in raw:
        entry_s = str(entry).strip()
        if entry_s not in allowed:
            raise ValueError(f"{where}.release_type entry {entry_s!r} not in {sorted(allowed)}")
        out.append(entry_s)
    return out


def _parse_optimizer_app(
    raw: dict, default_release_type: list[str], allowed: set[str], where: str
) -> OptimizerAppConfig:
    release_type = _parse_release_types(
        raw.get("release_type", default_release_type), allowed, where
    )
    return OptimizerAppConfig(
        enabled=bool(raw.get("enabled", True)),
        min_age_days=int(raw.get("min_age_days", 0)),
        release_type=release_type,
        allow_size_increase=bool(raw.get("allow_size_increase", True)),
        allow_quality_downgrade=bool(raw.get("allow_quality_downgrade", True)),
        ignore_completed_in_queue=bool(raw.get("ignore_completed_in_queue", True)),
        auto_import_downgrades=bool(raw.get("auto_import_downgrades", True)),
    )


def parse_optimizer(raw: dict) -> OptimizerConfig:
    pick_order = str(raw.get("pick_order", "random")).strip()
    if pick_order not in PICK_ORDERS:
        raise ValueError(f"optimizer.pick_order={pick_order!r} not in {sorted(PICK_ORDERS)}")

    process_interval_seconds = int(raw.get("process_interval_seconds", 15))
    if process_interval_seconds < 10:
        raise ValueError(
            f"optimizer.process_interval_seconds must be >= 10, got {process_interval_seconds}"
        )

    return OptimizerConfig(
        enabled=bool(raw.get("enabled", False)),
        queue_max=int(raw.get("queue_max", 5)),
        pick_order=pick_order,
        process_interval_seconds=process_interval_seconds,
        list_refresh_minutes=int(raw.get("list_refresh_minutes", 15)),
        reevaluate_after_days=int(raw.get("reevaluate_after_days", 30)),
        radarr=_parse_optimizer_app(
            raw.get("radarr", {}),
            ["digitalRelease", "dateAdded"],
            RADARR_RELEASE_TYPES,
            "optimizer.radarr",
        ),
        sonarr=_parse_optimizer_app(
            raw.get("sonarr", {}),
            ["airDateUtc", "dateAdded"],
            SONARR_RELEASE_TYPES,
            "optimizer.sonarr",
        ),
        topsis=_parse_topsis(raw.get("topsis", {})),
    )


def default_topsis() -> TopsisConfig:
    """Parse the bundled defaults' TOPSIS section. For tests and tools that need a config
    without going through the full env-dependent load_config()."""
    from optimizarr.config import _load_defaults

    return _parse_topsis(_load_defaults()["optimizer"]["topsis"])
