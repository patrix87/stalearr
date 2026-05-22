import logging
from datetime import UTC, datetime

from stalearr.config import AppConfig
from stalearr.dates import age_days
from stalearr.http import ArrClient

logger = logging.getLogger("stalearr")


def _reference_date(episode: dict, release_type: str) -> str | None:
    if release_type == "dateAdded":
        ep_file = episode.get("episodeFile")
        if ep_file:
            return ep_file.get("dateAdded")
        return None
    return episode.get(release_type)


def _is_candidate(episode: dict, config: AppConfig, now: datetime) -> tuple[bool, str]:
    if not episode.get("monitored", False):
        return False, "not monitored"

    # Never unmonitor wanted-but-undownloaded items; that would mean "give up on this".
    if not episode.get("hasFile", False):
        return False, "no file"

    if config.require_cutoff_met:
        ep_file = episode.get("episodeFile") or {}
        if ep_file.get("qualityCutoffNotMet", True):
            return False, "quality cutoff not met"

    ref = _reference_date(episode, config.release_type)
    age = age_days(ref, now)
    if age is None:
        return False, f"no {config.release_type} date"
    if age < config.days:
        return False, f"only {age:.1f}d since {config.release_type}"

    return True, f"{age:.1f}d since {config.release_type}"


def _label(series_by_id: dict[int, dict], episode: dict) -> str:
    series_id = episode.get("seriesId") or 0
    series = series_by_id.get(series_id, {})
    title = series.get("title", "?")
    season = episode.get("seasonNumber", 0)
    number = episode.get("episodeNumber", 0)
    return f"{title} S{season:02d}E{number:02d}"


def run(config: AppConfig, dry_run: bool) -> None:
    client = ArrClient(config.url, config.api_key)
    logger.info("[sonarr] fetching series from %s", config.url)
    series_list = client.get("/api/v3/series")
    series_by_id = {s["id"]: s for s in series_list}
    logger.info("[sonarr] %d series", len(series_list))

    now = datetime.now(UTC)
    to_unmonitor: list[tuple[int, str]] = []

    for series in series_list:
        episodes = client.get(f"/api/v3/episode?seriesId={series['id']}&includeEpisodeFile=true")
        for episode in episodes:
            ok, reason = _is_candidate(episode, config, now)
            if ok:
                line = f"{_label(series_by_id, episode)} - {reason}"
                to_unmonitor.append((episode["id"], line))

    if not to_unmonitor:
        logger.info("[sonarr] nothing to unmonitor")
        return

    action = "would unmonitor" if dry_run else "unmonitoring"
    logger.info("[sonarr] %s %d episodes:", action, len(to_unmonitor))
    for _, line in to_unmonitor:
        logger.info("[sonarr]   %s", line)

    if dry_run:
        return

    # PUT /api/v3/episode/monitor handles bulk updates.
    client.put(
        "/api/v3/episode/monitor",
        {"episodeIds": [eid for eid, _ in to_unmonitor], "monitored": False},
    )
    logger.info("[sonarr] done")
