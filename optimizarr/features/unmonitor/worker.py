"""Unmonitor worker: cron-scheduled pass that unmonitors items N days after release.

A single app-agnostic pass (run_app) serves both Radarr and Sonarr via the ArrApi clients;
the cron loop runs it on the configured schedule, with an optional run-on-start pass.
"""

import logging
import time
from datetime import UTC, datetime

from croniter import croniter

from optimizarr.arr import ArrApi, build_client
from optimizarr.config import Config
from optimizarr.features.unmonitor.candidates import is_candidate
from optimizarr.features.unmonitor.config import UnmonitorAppConfig

logger = logging.getLogger("optimizarr")


def run_app(api: ArrApi, cfg: UnmonitorAppConfig, dry_run: bool) -> None:
    """One unmonitor pass for a single app."""
    logger.info("[%s] fetching items", api.app)
    items = api.list_items()
    logger.info("[%s] %d items", api.app, len(items))

    now = datetime.now(UTC)
    to_unmonitor: list[tuple[int, str]] = []
    for item in items:
        ok, reason = is_candidate(api, item, cfg, now)
        if ok:
            to_unmonitor.append((api.item_id(item), f"{api.label(item)} - {reason}"))

    if not to_unmonitor:
        logger.info("[%s] nothing to unmonitor", api.app)
        return

    action = "would unmonitor" if dry_run else "unmonitoring"
    logger.info("[%s] %s %d items:", api.app, action, len(to_unmonitor))
    for _, line in to_unmonitor:
        logger.info("[%s]   %s", api.app, line)

    if dry_run:
        return

    api.set_monitored([item_id for item_id, _ in to_unmonitor], False)
    logger.info("[%s] done", api.app)


class UnmonitorWorker:
    def __init__(self, config: Config):
        self.um = config.unmonitor
        self.dry_run = config.dry_run
        conns = {"radarr": config.radarr, "sonarr": config.sonarr}
        cfgs = {"radarr": self.um.radarr, "sonarr": self.um.sonarr}
        self.apps: list[tuple[ArrApi, UnmonitorAppConfig]] = [
            (build_client(app, conn), cfgs[app]) for app, conn in conns.items() if conn is not None
        ]

    def run_once(self) -> None:
        for api, cfg in self.apps:
            try:
                run_app(api, cfg, self.dry_run)
            except Exception:
                logger.exception("[%s] unmonitor run failed", api.app)

    def run(self) -> None:
        if self.um.run_on_start:
            logger.info("Running initial unmonitor pass (run_on_start=true)")
            self.run_once()

        schedule = croniter(self.um.cron_schedule, datetime.now(UTC))
        while True:
            next_run = schedule.get_next(datetime)
            sleep_for = (next_run - datetime.now(UTC)).total_seconds()
            if sleep_for > 0:
                logger.info("Next unmonitor run at %s (in %.0fs)", next_run.isoformat(), sleep_for)
                time.sleep(sleep_for)
            self.run_once()
