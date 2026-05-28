import logging
import os
import tomllib
from dataclasses import dataclass, field

logger = logging.getLogger("optimizarr")

RADARR_RELEASE_TYPES = {
    "digitalRelease",
    "physicalRelease",
    "inCinemas",
    "releaseDate",
    "dateAdded",
}

SONARR_RELEASE_TYPES = {
    "airDateUtc",
    "dateAdded",
}

PICK_ORDERS = {"random", "ordered"}

DEFAULT_CONFIG_PATH = "/config/config.toml"
DEFAULT_STATE_PATH = "/data/state.json"


# ===== Connection (from env: secrets + URLs only) =====


@dataclass
class Connection:
    name: str
    url: str
    api_key: str


# ===== Unmonitor feature =====


@dataclass
class UnmonitorAppConfig:
    days: int = 30
    release_type: str = ""
    require_cutoff_met: bool = True


@dataclass
class UnmonitorConfig:
    enabled: bool = True
    cron_schedule: str = "0 4 * * *"
    run_on_start: bool = True
    radarr: UnmonitorAppConfig = field(default_factory=UnmonitorAppConfig)
    sonarr: UnmonitorAppConfig = field(default_factory=UnmonitorAppConfig)


# ===== Optimizer feature =====


@dataclass
class TopsisConfig:
    score_ideal: int = 1_000_000
    resolution_ideal: int = 2160
    score_anti_ideal: int = 0
    resolution_anti_ideal: int = 480
    score_floor_preferred: int = 900_000
    score_drop_from_top: int = 250_000
    min_closeness_gain: float = 0.05
    min_size_savings_gb: float = 0.5
    allow_upgrades: bool = True
    min_closeness_gain_for_upgrade: float = 0.10
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
class OptimizerConfig:
    enabled: bool = False
    apps: list[str] = field(default_factory=lambda: ["radarr", "sonarr"])
    queue_max: int = 0
    pick_order: str = "random"
    process_interval_seconds: int = 10
    queue_recheck_seconds: int = 60
    list_refresh_minutes: int = 60
    reevaluate_after_days: int = 30
    topsis: TopsisConfig = field(default_factory=TopsisConfig)


@dataclass
class Config:
    dry_run: bool
    log_level: str
    state_path: str
    radarr: Connection | None
    sonarr: Connection | None
    unmonitor: UnmonitorConfig
    optimizer: OptimizerConfig


# ===== env helpers (secrets, URLs, paths only) =====


def _load_connection(prefix: str) -> Connection | None:
    url = os.environ.get(f"{prefix}_URL", "").strip().rstrip("/")
    api_key = os.environ.get(f"{prefix}_API_KEY", "").strip()
    if not url or not api_key:
        return None
    return Connection(name=prefix.lower(), url=url, api_key=api_key)


# ===== TOML parsing helpers =====


def _int_keyed(raw: dict, where: str) -> dict[int, object]:
    out: dict[int, object] = {}
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
    for key in (
        "min_closeness_gain",
        "min_size_savings_gb",
        "min_closeness_gain_for_upgrade",
    ):
        if key in raw:
            setattr(cfg, key, float(raw[key]))
    if "allow_upgrades" in raw:
        cfg.allow_upgrades = bool(raw["allow_upgrades"])

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
                for res, v in _int_keyed(
                    inner, f"size_envelope_by_profile.{name}"
                ).items()
            }
            for name, inner in raw["size_envelope_by_profile"].items()
        }

    _validate_weights("weights", cfg.weights)
    for name, w in cfg.weights_by_profile.items():
        _validate_weights(f"weights_by_profile.{name}", w)
    return cfg


def _validate_weights(where: str, w: dict[str, float]) -> None:
    missing = {"score", "resolution", "size"} - w.keys()
    if missing:
        raise ValueError(f"{where}: missing weight keys {sorted(missing)}")
    total = sum(w.values())
    if abs(total - 1.0) > 0.01:
        raise ValueError(f"{where}: weights must sum to 1.0, got {total:.3f}")


def _parse_unmonitor_app(
    raw: dict, default_release_type: str, allowed: set[str], where: str
) -> UnmonitorAppConfig:
    release_type = str(raw.get("release_type", default_release_type)).strip()
    if release_type not in allowed:
        raise ValueError(f"{where}.release_type={release_type!r} not in {sorted(allowed)}")
    return UnmonitorAppConfig(
        days=int(raw.get("days", 30)),
        release_type=release_type,
        require_cutoff_met=bool(raw.get("require_cutoff_met", True)),
    )


def _parse_unmonitor(raw: dict) -> UnmonitorConfig:
    return UnmonitorConfig(
        enabled=bool(raw.get("enabled", True)),
        cron_schedule=str(raw.get("cron_schedule", "0 4 * * *")).strip(),
        run_on_start=bool(raw.get("run_on_start", True)),
        radarr=_parse_unmonitor_app(
            raw.get("radarr", {}), "digitalRelease", RADARR_RELEASE_TYPES, "unmonitor.radarr"
        ),
        sonarr=_parse_unmonitor_app(
            raw.get("sonarr", {}), "airDateUtc", SONARR_RELEASE_TYPES, "unmonitor.sonarr"
        ),
    )


def _parse_optimizer(raw: dict) -> OptimizerConfig:
    apps = raw.get("apps", ["radarr", "sonarr"])
    if not isinstance(apps, list) or any(a not in ("radarr", "sonarr") for a in apps):
        raise ValueError(f"optimizer.apps must be a list from ['radarr','sonarr'], got {apps!r}")

    pick_order = str(raw.get("pick_order", "random")).strip()
    if pick_order not in PICK_ORDERS:
        raise ValueError(f"optimizer.pick_order={pick_order!r} not in {sorted(PICK_ORDERS)}")

    return OptimizerConfig(
        enabled=bool(raw.get("enabled", False)),
        apps=apps,
        queue_max=int(raw.get("queue_max", 0)),
        pick_order=pick_order,
        process_interval_seconds=int(raw.get("process_interval_seconds", 10)),
        queue_recheck_seconds=int(raw.get("queue_recheck_seconds", 60)),
        list_refresh_minutes=int(raw.get("list_refresh_minutes", 60)),
        reevaluate_after_days=int(raw.get("reevaluate_after_days", 30)),
        topsis=_parse_topsis(raw.get("topsis", {})),
    )


def load_config(config_path: str | None = None) -> Config:
    radarr = _load_connection("RADARR")
    sonarr = _load_connection("SONARR")
    if radarr is None and sonarr is None:
        raise ValueError(
            "Neither Radarr nor Sonarr is configured. "
            "Set RADARR_URL+RADARR_API_KEY and/or SONARR_URL+SONARR_API_KEY."
        )

    path = config_path or os.environ.get("CONFIG_PATH", DEFAULT_CONFIG_PATH)
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as e:
        raise ValueError(f"Config file not found at {path!r} (set CONFIG_PATH to override)") from e
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Config file {path!r} is not valid TOML: {e}") from e

    optimizer = _parse_optimizer(raw.get("optimizer", {}))
    configured = {name for name, c in (("radarr", radarr), ("sonarr", sonarr)) if c is not None}
    optimizer.apps = [a for a in optimizer.apps if a in configured]

    return Config(
        dry_run=bool(raw.get("dry_run", False)),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        state_path=os.environ.get("STATE_PATH", DEFAULT_STATE_PATH),
        radarr=radarr,
        sonarr=sonarr,
        unmonitor=_parse_unmonitor(raw.get("unmonitor", {})),
        optimizer=optimizer,
    )


def log_summary(config: Config) -> None:
    logger.info("Dry run: %s", config.dry_run)
    logger.info("State path: %s", config.state_path)
    for conn in (config.radarr, config.sonarr):
        if conn is not None:
            logger.info("%s: url=%s", conn.name, conn.url)

    um = config.unmonitor
    logger.info(
        "Unmonitor: enabled=%s cron=%s run_on_start=%s",
        um.enabled,
        um.cron_schedule,
        um.run_on_start,
    )
    if um.enabled:
        for name, app in (("radarr", um.radarr), ("sonarr", um.sonarr)):
            logger.info(
                "  unmonitor.%s: days=%d release_type=%s require_cutoff_met=%s",
                name,
                app.days,
                app.release_type,
                app.require_cutoff_met,
            )

    opt = config.optimizer
    logger.info(
        "Optimizer: enabled=%s apps=%s queue_max=%d pick_order=%s",
        opt.enabled,
        opt.apps,
        opt.queue_max,
        opt.pick_order,
    )
    if opt.enabled:
        logger.info(
            "  optimizer: process_interval=%ds queue_recheck=%ds "
            "list_refresh=%dm reevaluate_after=%dd",
            opt.process_interval_seconds,
            opt.queue_recheck_seconds,
            opt.list_refresh_minutes,
            opt.reevaluate_after_days,
        )
