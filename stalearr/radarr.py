import logging
from datetime import UTC, datetime

from stalearr.config import AppConfig
from stalearr.dates import age_days
from stalearr.http import ArrClient

logger = logging.getLogger("stalearr")


def _reference_date(movie: dict, release_type: str) -> str | None:
    if release_type == "dateAdded":
        movie_file = movie.get("movieFile")
        if movie_file:
            return movie_file.get("dateAdded")
        return None
    return movie.get(release_type)


def _is_candidate(movie: dict, config: AppConfig, now: datetime) -> tuple[bool, str]:
    if not movie.get("monitored", False):
        return False, "not monitored"

    # Never unmonitor wanted-but-undownloaded items; that would mean "give up on this".
    if not movie.get("hasFile", False):
        return False, "no file"

    if config.require_cutoff_met:
        movie_file = movie.get("movieFile") or {}
        if movie_file.get("qualityCutoffNotMet", True):
            return False, "quality cutoff not met"

    ref = _reference_date(movie, config.release_type)
    age = age_days(ref, now)
    if age is None:
        return False, f"no {config.release_type} date"
    if age < config.days:
        return False, f"only {age:.1f}d since {config.release_type}"

    return True, f"{age:.1f}d since {config.release_type}"


def run(config: AppConfig, dry_run: bool) -> None:
    client = ArrClient(config.url, config.api_key)
    logger.info("[radarr] fetching movies from %s", config.url)
    movies = client.get("/api/v3/movie")
    logger.info("[radarr] %d total movies", len(movies))

    now = datetime.now(UTC)
    to_unmonitor: list[tuple[int, str]] = []

    for movie in movies:
        ok, reason = _is_candidate(movie, config, now)
        if ok:
            label = f"{movie.get('title')} ({movie.get('year')})"
            to_unmonitor.append((movie["id"], f"{label} - {reason}"))

    if not to_unmonitor:
        logger.info("[radarr] nothing to unmonitor")
        return

    action = "would unmonitor" if dry_run else "unmonitoring"
    logger.info("[radarr] %s %d movies:", action, len(to_unmonitor))
    for _, line in to_unmonitor:
        logger.info("[radarr]   %s", line)

    if dry_run:
        return

    client.put(
        "/api/v3/movie/editor",
        {"movieIds": [mid for mid, _ in to_unmonitor], "monitored": False},
    )
    logger.info("[radarr] done")
