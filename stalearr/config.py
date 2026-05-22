import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("stalearr")

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


@dataclass
class AppConfig:
    name: str
    url: str
    api_key: str
    days: int
    release_type: str
    require_cutoff_met: bool


@dataclass
class Config:
    cron_schedule: str
    run_on_start: bool
    dry_run: bool
    log_level: str
    radarr: AppConfig | None
    sonarr: AppConfig | None


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _load_app(prefix: str, default_release_type: str, allowed_types: set[str]) -> AppConfig | None:
    url = os.environ.get(f"{prefix}_URL", "").strip().rstrip("/")
    api_key = os.environ.get(f"{prefix}_API_KEY", "").strip()
    if not url or not api_key:
        return None

    release_type = os.environ.get(f"{prefix}_RELEASE_TYPE", default_release_type).strip()
    if release_type not in allowed_types:
        raise ValueError(f"{prefix}_RELEASE_TYPE={release_type!r} not in {sorted(allowed_types)}")

    return AppConfig(
        name=prefix.lower(),
        url=url,
        api_key=api_key,
        days=_env_int(f"{prefix}_DAYS", 30),
        release_type=release_type,
        require_cutoff_met=_env_bool(f"{prefix}_REQUIRE_CUTOFF_MET", True),
    )


def load_config() -> Config:
    radarr = _load_app("RADARR", "digitalRelease", RADARR_RELEASE_TYPES)
    sonarr = _load_app("SONARR", "airDateUtc", SONARR_RELEASE_TYPES)

    if radarr is None and sonarr is None:
        raise ValueError(
            "Neither Radarr nor Sonarr is configured. "
            "Set RADARR_URL+RADARR_API_KEY and/or SONARR_URL+SONARR_API_KEY."
        )

    return Config(
        cron_schedule=os.environ.get("CRON_SCHEDULE", "0 4 * * *").strip(),
        run_on_start=_env_bool("RUN_ON_START", True),
        dry_run=_env_bool("DRY_RUN", False),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        radarr=radarr,
        sonarr=sonarr,
    )


def log_summary(config: Config) -> None:
    logger.info("Cron schedule: %s", config.cron_schedule)
    logger.info("Run on start: %s", config.run_on_start)
    logger.info("Dry run: %s", config.dry_run)
    for app in (config.radarr, config.sonarr):
        if app is None:
            continue
        logger.info(
            "%s: url=%s days=%d release_type=%s require_cutoff_met=%s",
            app.name,
            app.url,
            app.days,
            app.release_type,
            app.require_cutoff_met,
        )
