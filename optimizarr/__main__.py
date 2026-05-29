import logging
import sys
import threading

from croniter import croniter

from optimizarr.config import load_config, log_summary
from optimizarr.features.optimizer.state import StateManager
from optimizarr.features.optimizer.worker import OptimizerWorker
from optimizarr.features.unmonitor.worker import UnmonitorWorker

logger = logging.getLogger("optimizarr")


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

        UnmonitorWorker(config).run()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    return 0


if __name__ == "__main__":
    sys.exit(main())
