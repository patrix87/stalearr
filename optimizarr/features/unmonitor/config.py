"""Unmonitor feature configuration: schema + parsing of the [unmonitor] TOML section.

The shared loader (optimizarr.config) delegates to parse_unmonitor() here, so the unmonitor
feature owns its own config surface.
"""

from dataclasses import dataclass, field

from optimizarr.config import RADARR_RELEASE_TYPES, SONARR_RELEASE_TYPES


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


def parse_unmonitor(raw: dict) -> UnmonitorConfig:
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
