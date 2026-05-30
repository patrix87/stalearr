"""Optimizer worker: walk the library, re-pick better releases, grab them.

"Optimized" means the algorithm can no longer find anything better than the current file
(HOLD) — never merely "we triggered a grab". The worker is deliberately simple:

  - refresh the item list on a slow interval (list_refresh_minutes);
  - on each tick, if the download queue is at/under queue_max, pick a not-yet-satisfied
    item that isn't already in the queue, evaluate it, and either grab a better release
    or mark it satisfied (HOLD);
  - a grab is never recorded. Success shows up as a HOLD on the next evaluation (→
    satisfied); failure leaves the item unsatisfied so it's retried later, with the failed
    release now blocklisted by Radarr/Sonarr's Failed Download Handling.

Downloads in progress are read live from the queue (gate + per-item skip), so there's no
in-flight bookkeeping and a restart needs no reconciliation. The per-item decision lives in
.decision; app-specific HTTP lives behind the optimizarr.arr clients; the loop here is
app-agnostic.
"""

import logging
import threading
from datetime import UTC, datetime

from optimizarr.arr import ArrApi, build_client
from optimizarr.config import Config
from optimizarr.dates import age_days
from optimizarr.features.optimizer.config import OptimizerAppConfig, OptimizerConfig
from optimizarr.features.optimizer.decision import decide, format_decision
from optimizarr.features.optimizer.state import StateManager
from optimizarr.features.optimizer.topsis import Topsis

logger = logging.getLogger("optimizarr")


def age_ok(api: ArrApi, item: dict, app_cfg: OptimizerAppConfig, now: datetime) -> bool:
    """True if the item is old enough to consider. With min_age_days <= 0 there is no gate.
    When gating is on and the release date is unknown, the item is skipped."""
    if app_cfg.min_age_days <= 0:
        return True
    age = age_days(api.reference_date(item, app_cfg.release_type), now)
    return age is not None and age >= app_cfg.min_age_days


class _AppContext:
    """Per-app worker state: client, its config, cached item list, active pool."""

    def __init__(self, adapter: ArrApi, app_cfg: OptimizerAppConfig):
        self.adapter = adapter
        self.app_cfg = app_cfg
        self.items_by_id: dict[int, dict] = {}
        self.pool: list[int] = []
        self.evaluated: set[int] = set()
        self.last_refresh: datetime | None = None

    def needs_refresh(self, now: datetime, list_refresh_minutes: int) -> bool:
        if self.last_refresh is None:
            return True
        age_min = (now - self.last_refresh).total_seconds() / 60
        return age_min >= list_refresh_minutes


class OptimizerWorker:
    def __init__(self, config: Config, state: StateManager):
        self.config = config
        self.opt: OptimizerConfig = config.optimizer
        self.state = state
        self.topsis = Topsis(self.opt.topsis)
        self.dry_run = config.dry_run
        self._stop = threading.Event()

        conns = {"radarr": config.radarr, "sonarr": config.sonarr}
        app_cfgs = {"radarr": self.opt.radarr, "sonarr": self.opt.sonarr}
        self.contexts: dict[str, _AppContext] = {}
        for app, conn in conns.items():
            if conn is None or not app_cfgs[app].enabled:
                continue
            self.contexts[app] = _AppContext(build_client(app, conn), app_cfgs[app])

    def stop(self) -> None:
        self._stop.set()

    # ----- per-app machinery -----

    def _refresh(self, ctx: _AppContext, now: datetime) -> None:
        adapter = ctx.adapter
        adapter.refresh_profiles()
        # Select on hasFile alone (not monitored): the optimizer improves the existing
        # library, and the unmonitor feature deliberately strips monitoring once a file
        # exists. The age gate is the optimizer's own min_age_days.
        items = [
            it
            for it in adapter.list_items()
            if adapter.has_file(it) and age_ok(adapter, it, ctx.app_cfg, now)
        ]
        ctx.items_by_id = {adapter.item_id(it): it for it in items}
        # NB: ctx.evaluated is intentionally NOT cleared here. A refresh only updates the
        # candidate set (new items become pickable, removed ones drop); the current pass
        # keeps its progress so a slow walk over a large library isn't restarted every
        # list_refresh_minutes. The pass resets in _build_pool once it's fully covered.
        ctx.last_refresh = now
        logger.info("[%s] list refreshed: %d items with files", adapter.app, len(items))

    def _build_pool(self, ctx: _AppContext, now: datetime) -> None:
        days = self.opt.reevaluate_after_days
        app = ctx.adapter.app

        def active(exclude_evaluated: bool) -> list[int]:
            return [
                item_id
                for item_id in ctx.items_by_id
                if self.state.is_active(app, item_id, now, days)
                and not (exclude_evaluated and item_id in ctx.evaluated)
            ]

        ctx.pool = active(exclude_evaluated=True)
        if not ctx.pool and ctx.evaluated:
            # Every active item has been evaluated this pass — reset and start a new one.
            ctx.evaluated.clear()
            ctx.pool = active(exclude_evaluated=False)

        if self.opt.pick_order == "random":
            import random

            random.shuffle(ctx.pool)

    def _process_one(self, ctx: _AppContext, item_id: int) -> None:
        adapter = ctx.adapter
        item = ctx.items_by_id[item_id]
        runtime_h = adapter.runtime_h(item)
        profile_name, target_res = adapter.profile_for(item)
        current_file = adapter.current_file(item)
        releases = adapter.releases(item)

        decision = decide(
            self.topsis,
            releases,
            runtime_h,
            profile_name,
            target_res,
            current_file,
            allow_size_increase=ctx.app_cfg.allow_size_increase,
            allow_quality_downgrade=ctx.app_cfg.allow_quality_downgrade,
        )
        label = adapter.label(item)
        logger.info("%s", format_decision(adapter.app, label, decision, self.dry_run))

        if decision.action == "HOLD":
            # Nothing better (incl. no viable release): drop it from the pool.
            if not self.dry_run:
                self.state.mark_satisfied(adapter.app, item_id)
            return

        # ACT: grab, but do NOT record anything. If the download succeeds, the next
        # evaluation HOLDs and marks it satisfied; if it fails, the item stays in the pool
        # and is retried later (the failed release now blocklisted). Re-evaluation is the
        # only success/failure signal we need.
        if not self.dry_run:
            adapter.grab(decision.release or {})

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    # ----- main loop -----

    def run(self) -> None:
        if not self.contexts:
            logger.info("[optimizer] no configured apps; worker exiting")
            return
        logger.info("[optimizer] worker started for apps=%s", list(self.contexts))

        while not self._stop.is_set():
            progressed = False
            for ctx in self.contexts.values():
                if self._stop.is_set():
                    break
                if self._process_app_once(ctx):
                    progressed = True
                    self._sleep(self.opt.process_interval_seconds)

            if not progressed:
                # Nothing actionable (queue full or pool exhausted): wait one short tick.
                self._sleep(self.opt.process_interval_seconds)

    def _process_app_once(self, ctx: _AppContext) -> bool:
        """Do at most one unit of work for an app. Returns True if an item was processed."""
        now = datetime.now(UTC)
        adapter = ctx.adapter

        if ctx.needs_refresh(now, self.opt.list_refresh_minutes):
            self._refresh(ctx, now)
            ctx.pool = []  # force rebuild below

        # One queue fetch serves both the global gate and the "is this item currently
        # downloading?" skip below — no in-flight state needed.
        queue_count, queue_ids = adapter.queue()

        if not ctx.pool:
            self._build_pool(ctx, now)
        if not ctx.pool:
            return False

        if queue_count > self.opt.queue_max:
            logger.debug(
                "[%s] queue %d > max %d; waiting", adapter.app, queue_count, self.opt.queue_max
            )
            return False

        item_id = ctx.pool.pop()
        if item_id in queue_ids:
            return False  # already downloading; skip and move on

        ctx.evaluated.add(item_id)  # don't re-pick within this refresh cycle
        try:
            self._process_one(ctx, item_id)
        except Exception:
            logger.exception("[%s] failed to process id=%d", adapter.app, item_id)
        return True


def run_optimizer(config: Config, state: StateManager) -> OptimizerWorker:
    """Construct and run the worker (blocking). Returns the worker (for tests/stop)."""
    worker = OptimizerWorker(config, state)
    worker.run()
    return worker
