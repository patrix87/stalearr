import logging
import sys
import threading
import time
from datetime import UTC, datetime

from croniter import croniter

import optimizarr.radarr as radarr_mod
import optimizarr.sonarr as sonarr_mod
from optimizarr.config import Config, load_config, log_summary
from optimizarr.optimizer import OptimizerWorker
from optimizarr.state import StateManager

logger = logging.getLogger("optimizarr")


def run_unmonitor(config: Config) -> None:
    if config.radarr is not None:
        try:
            radarr_mod.run(config.radarr, config.unmonitor.radarr, config.dry_run)
        except Exception:
            logger.exception("[radarr] unmonitor run failed")

    if config.sonarr is not None:
        try:
            sonarr_mod.run(config.sonarr, config.unmonitor.sonarr, config.dry_run)
        except Exception:
            logger.exception("[sonarr] unmonitor run failed")


def _unmonitor_loop(config: Config) -> None:
    um = config.unmonitor
    if um.run_on_start:
        logger.info("Running initial unmonitor pass (run_on_start=true)")
        run_unmonitor(config)

    schedule = croniter(um.cron_schedule, datetime.now(UTC))
    while True:
        next_run = schedule.get_next(datetime)
        sleep_for = (next_run - datetime.now(UTC)).total_seconds()
        if sleep_for > 0:
            logger.info("Next unmonitor run at %s (in %.0fs)", next_run.isoformat(), sleep_for)
            time.sleep(sleep_for)
        run_unmonitor(config)


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
    logger.info("optimizarr starting")
    log_summary(config)

    um = config.unmonitor
    opt = config.optimizer

    if um.enabled and not croniter.is_valid(um.cron_schedule):
        logger.error("unmonitor.cron_schedule %r is not a valid cron expression", um.cron_schedule)
        return 1

    optimizer_enabled = opt.enabled and bool(opt.apps)
    if not um.enabled and not optimizer_enabled:
        logger.info("Both unmonitor and optimizer are disabled; nothing to do")
        return 0

    # Optimizer runs on its own daemon thread; unmonitor cron runs on the main thread.
    try:
        if optimizer_enabled:
            state = StateManager(config.state_path)
            worker = OptimizerWorker(config, state)
            thread = threading.Thread(target=worker.run, name="optimizer", daemon=True)
            thread.start()
            if not um.enabled:
                thread.join()
                return 0

        _unmonitor_loop(config)
    except KeyboardInterrupt:
        logger.info("Shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
