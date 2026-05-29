"""Optimizer feature configuration: schema + parsing of the [optimizer] TOML section.

The shared loader (optimizarr.config) delegates to parse_optimizer() here, so the optimizer
owns its own config surface.
"""

from dataclasses import dataclass, field
from typing import Any

from optimizarr.config import RADARR_RELEASE_TYPES, SONARR_RELEASE_TYPES

PICK_ORDERS = {"random", "ordered"}


@dataclass
class TopsisConfig:
    score_ideal: int = 1_000_000
    resolution_ideal: int = 2160
    score_anti_ideal: int = 0
    resolution_anti_ideal: int = 480
    score_floor_preferred: int = 900_000
    score_drop_from_top: int = 250_000
    # Swap when the pick improves overall closeness by at least this much. Closeness
    # already folds in score, resolution, and size (per-profile envelope + weights), so
    # this single small threshold covers both shrinks and quality upgrades.
    min_closeness_gain: float = 0.02
    weights: dict[str, float] = field(
        default_factory=lambda: {"score": 0.40, "resolution": 0.25, "size": 0.35}
    )
    weights_by_profile: dict[str, dict[str, float]] = field(
        default_factory=lambda: {
            "2160p Quality": {"score": 0.60, "resolution": 0.15, "size": 0.25},
        }
    )
    sanity_gbh_floor_by_resolution: dict[int, float] = field(
        default_factory=lambda: {480: 0.2, 720: 0.4, 1080: 0.8, 2160: 1.5}
    )
    size_envelope_by_resolution: dict[int, tuple[float, float]] = field(
        default_factory=lambda: {
            480: (0.5, 5.0),
            720: (1.5, 8.0),
            1080: (3.0, 12.0),
            2160: (6.0, 25.0),
        }
    )
    size_envelope_by_profile: dict[str, dict[int, tuple[float, float]]] = field(
        default_factory=lambda: {
            "1080p Efficient": {1080: (3.0, 12.0)},
            "2160p Efficient": {2160: (6.0, 25.0)},
            "2160p Quality": {2160: (12.0, 40.0)},
        }
    )


@dataclass
class OptimizerAppConfig:
    # Only consider items at least this many days past their release date.
    # 0 = no age gate (consider everything with a file). release_type selects which
    # date field the age is measured from (same fields as the unmonitor feature).
    min_age_days: int = 0
    release_type: str = ""


@dataclass
class OptimizerConfig:
    enabled: bool = False
    apps: list[str] = field(default_factory=lambda: ["radarr", "sonarr"])
    queue_max: int = 5
    pick_order: str = "random"
    process_interval_seconds: int = 15
    list_refresh_minutes: int = 15
    reevaluate_after_days: int = 30
    radarr: OptimizerAppConfig = field(
        default_factory=lambda: OptimizerAppConfig(release_type="digitalRelease")
    )
    sonarr: OptimizerAppConfig = field(
        default_factory=lambda: OptimizerAppConfig(release_type="airDateUtc")
    )
    topsis: TopsisConfig = field(default_factory=TopsisConfig)


# ----- parsing helpers -----


def _int_keyed(raw: dict, where: str) -> dict[int, Any]:
    out: dict[int, Any] = {}
    for k, v in raw.items():
        try:
            out[int(k)] = v
        except (TypeError, ValueError) as e:
            raise ValueError(f"{where}: key {k!r} is not an integer resolution") from e
    return out


def _envelope_pair(value: object, where: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{where}: expected [target, bloat] pair, got {value!r}")
    return (float(value[0]), float(value[1]))


def _validate_weights(where: str, w: dict[str, float]) -> None:
    missing = {"score", "resolution", "size"} - w.keys()
    if missing:
        raise ValueError(f"{where}: missing weight keys {sorted(missing)}")
    total = sum(w.values())
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"{where}: weights must sum to 1.0, got {total:.3f}")


def _parse_topsis(raw: dict) -> TopsisConfig:
    cfg = TopsisConfig()
    for key in (
        "score_ideal",
        "resolution_ideal",
        "score_anti_ideal",
        "resolution_anti_ideal",
        "score_floor_preferred",
        "score_drop_from_top",
    ):
        if key in raw:
            setattr(cfg, key, int(raw[key]))
    if "min_closeness_gain" in raw:
        cfg.min_closeness_gain = float(raw["min_closeness_gain"])

    if "weights" in raw:
        cfg.weights = {k: float(v) for k, v in raw["weights"].items()}
    if "weights_by_profile" in raw:
        cfg.weights_by_profile = {
            name: {k: float(v) for k, v in w.items()}
            for name, w in raw["weights_by_profile"].items()
        }
    if "sanity_gbh_floor_by_resolution" in raw:
        cfg.sanity_gbh_floor_by_resolution = {
            res: float(v)
            for res, v in _int_keyed(
                raw["sanity_gbh_floor_by_resolution"], "sanity_gbh_floor_by_resolution"
            ).items()
        }
    if "size_envelope_by_resolution" in raw:
        cfg.size_envelope_by_resolution = {
            res: _envelope_pair(v, f"size_envelope_by_resolution.{res}")
            for res, v in _int_keyed(
                raw["size_envelope_by_resolution"], "size_envelope_by_resolution"
            ).items()
        }
    if "size_envelope_by_profile" in raw:
        cfg.size_envelope_by_profile = {
            name: {
                res: _envelope_pair(v, f"size_envelope_by_profile.{name}.{res}")
                for res, v in _int_keyed(inner, f"size_envelope_by_profile.{name}").items()
            }
            for name, inner in raw["size_envelope_by_profile"].items()
        }

    _validate_weights("weights", cfg.weights)
    for name, w in cfg.weights_by_profile.items():
        _validate_weights(f"weights_by_profile.{name}", w)
    return cfg


def _parse_optimizer_app(
    raw: dict, default_release_type: str, allowed: set[str], where: str
) -> OptimizerAppConfig:
    release_type = str(raw.get("release_type", default_release_type)).strip()
    if release_type not in allowed:
        raise ValueError(f"{where}.release_type={release_type!r} not in {sorted(allowed)}")
    return OptimizerAppConfig(
        min_age_days=int(raw.get("min_age_days", 0)),
        release_type=release_type,
    )


def parse_optimizer(raw: dict) -> OptimizerConfig:
    apps = raw.get("apps", ["radarr", "sonarr"])
    if not isinstance(apps, list) or any(a not in ("radarr", "sonarr") for a in apps):
        raise ValueError(f"optimizer.apps must be a list from ['radarr','sonarr'], got {apps!r}")

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
        apps=apps,
        queue_max=int(raw.get("queue_max", 5)),
        pick_order=pick_order,
        process_interval_seconds=process_interval_seconds,
        list_refresh_minutes=int(raw.get("list_refresh_minutes", 15)),
        reevaluate_after_days=int(raw.get("reevaluate_after_days", 30)),
        radarr=_parse_optimizer_app(
            raw.get("radarr", {}), "digitalRelease", RADARR_RELEASE_TYPES, "optimizer.radarr"
        ),
        sonarr=_parse_optimizer_app(
            raw.get("sonarr", {}), "airDateUtc", SONARR_RELEASE_TYPES, "optimizer.sonarr"
        ),
        topsis=_parse_topsis(raw.get("topsis", {})),
    )
