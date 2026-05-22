import logging
import sys
import time
from datetime import UTC, datetime

from croniter import croniter

import stalearr.radarr as radarr_mod
import stalearr.sonarr as sonarr_mod
from stalearr.config import Config, load_config, log_summary

logger = logging.getLogger("stalearr")


def run_once(config: Config) -> None:
    if config.radarr is not None:
        try:
            radarr_mod.run(config.radarr, config.dry_run)
        except Exception:
            logger.exception("[radarr] run failed")

    if config.sonarr is not None:
        try:
            sonarr_mod.run(config.sonarr, config.dry_run)
        except Exception:
            logger.exception("[sonarr] run failed")


def main() -> int:
    try:
        config = load_config()
    except ValueError as e:
        logging.basicConfig(level="INFO", format="%(asctime)s [%(levelname)s] %(message)s")
        logger.error("%s", e)
        return 1

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger.info("stalearr starting")
    log_summary(config)

    if not croniter.is_valid(config.cron_schedule):
        logger.error("CRON_SCHEDULE %r is not a valid cron expression", config.cron_schedule)
        return 1

    if config.run_on_start:
        logger.info("Running initial pass (RUN_ON_START=true)")
        run_once(config)

    schedule = croniter(config.cron_schedule, datetime.now(UTC))
    while True:
        next_run = schedule.get_next(datetime)
        sleep_for = (next_run - datetime.now(UTC)).total_seconds()
        if sleep_for > 0:
            logger.info("Next run at %s (in %.0fs)", next_run.isoformat(), sleep_for)
            time.sleep(sleep_for)
        run_once(config)


if __name__ == "__main__":
    sys.exit(main())
