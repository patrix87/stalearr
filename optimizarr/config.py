from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from optimizarr.features.optimizer.config import OptimizerConfig
    from optimizarr.features.unmonitor.config import UnmonitorConfig

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

# In-container paths. optimizarr runs in Docker with /config and /data mounted; these
# are not env-configurable. Edit them here if you ever run outside a container.
CONFIG_PATH = "/config/config.toml"
STATE_PATH = "/data/state.json"


# ===== Connection (from env: secrets + URLs only) =====


@dataclass
class Connection:
    name: str
    url: str
    api_key: str


@dataclass
class Config:
    dry_run: bool
    log_level: str
    state_path: str
    radarr: Connection | None
    sonarr: Connection | None
    unmonitor: UnmonitorConfig
    optimizer: OptimizerConfig


# ===== env helpers (secrets + URLs only) =====


def _load_connection(prefix: str) -> Connection | None:
    url = os.environ.get(f"{prefix}_URL", "").strip().rstrip("/")
    api_key = os.environ.get(f"{prefix}_API_KEY", "").strip()
    if not url or not api_key:
        return None
    return Connection(name=prefix.lower(), url=url, api_key=api_key)


def load_config(config_path: str | None = None) -> Config:
    from optimizarr.features.optimizer.config import parse_optimizer
    from optimizarr.features.unmonitor.config import parse_unmonitor

    radarr = _load_connection("RADARR")
    sonarr = _load_connection("SONARR")
    if radarr is None and sonarr is None:
        raise ValueError(
            "Neither Radarr nor Sonarr is configured. "
            "Set RADARR_URL+RADARR_API_KEY and/or SONARR_URL+SONARR_API_KEY."
        )

    path = config_path or CONFIG_PATH
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError as e:
        raise ValueError(f"Config file not found at {path!r}") from e
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Config file {path!r} is not valid TOML: {e}") from e

    optimizer = parse_optimizer(raw.get("optimizer", {}))
    configured = {name for name, c in (("radarr", radarr), ("sonarr", sonarr)) if c is not None}
    optimizer.apps = [a for a in optimizer.apps if a in configured]

    return Config(
        dry_run=bool(raw.get("dry_run", False)),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        state_path=STATE_PATH,
        radarr=radarr,
        sonarr=sonarr,
        unmonitor=parse_unmonitor(raw.get("unmonitor", {})),
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
            "  optimizer: process_interval=%ds list_refresh=%dm reevaluate_after=%dd",
            opt.process_interval_seconds,
            opt.list_refresh_minutes,
            opt.reevaluate_after_days,
        )
