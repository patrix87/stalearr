"""Optimizer worker: walk the library, re-pick better releases, grab them.

"Optimized" means the algorithm can no longer find anything better than the current
file (HOLD) — never merely "we triggered a grab". Built around the fact that grabs
frequently fail: in-flight is detected from the download queue, and a failed grab
(blocklisted by Radarr/Sonarr) is simply walked past on the next pass.

App-specific HTTP lives in the RadarrOptimizer / SonarrOptimizer adapters; the worker
loop and the per-item decision are app-agnostic.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from optimizarr.config import Config, Connection, OptimizerConfig
from optimizarr.http import ArrClient
from optimizarr.state import StateManager
from optimizarr.topsis import Topsis, max_allowed_resolution

logger = logging.getLogger("optimizarr")


@dataclass
class Decision:
    action: str  # "ACT" or "HOLD"
    reason: str
    release: dict | None = None
    pick_closeness: float | None = None
    current_closeness: float | None = None
    diag: dict | None = None


def decide(
    topsis: Topsis,
    releases: list[dict],
    runtime_h: float,
    profile_name: str | None,
    target_resolution: int | None,
    current_file: dict | None,
) -> Decision:
    """Pure decision: given fetched data, return ACT (with the release) or HOLD."""
    current_file_score = (current_file or {}).get("customFormatScore")
    pick, diag = topsis.pick(
        releases, runtime_h, profile_name, target_resolution, current_file_score
    )
    if pick is None:
        return Decision("HOLD", f"no candidate ({diag['score_floor_tier']})", diag=diag)

    release, _attrs, pick_closeness = pick
    current_closeness, _ = topsis.closeness_for_current_file(
        current_file or {}, runtime_h, profile_name, target_resolution
    )
    pick_size = release.get("size", 0)
    current_size = (current_file or {}).get("size", 0) or 0

    if topsis.should_swap(pick_closeness, current_closeness, pick_size, current_size):
        return Decision(
            "ACT",
            "better release available",
            release=release,
            pick_closeness=pick_closeness,
            current_closeness=current_closeness,
            diag=diag,
        )
    return Decision(
        "HOLD",
        "nothing better than current file",
        pick_closeness=pick_closeness,
        current_closeness=current_closeness,
        diag=diag,
    )


# ===== App adapters =====


class ArrOptimizer:
    """Base adapter. Subclasses wire the Radarr/Sonarr endpoints."""

    app: str

    def __init__(self, conn: Connection):
        self.client = ArrClient(conn.url, conn.api_key)
        self._profiles: dict[int, tuple[str, int]] = {}

    def refresh_profiles(self) -> None:
        profiles = self.client.get("/api/v3/qualityprofile") or []
        self._profiles = {
            p["id"]: (p.get("name", str(p["id"])), max_allowed_resolution(p.get("items")))
            for p in profiles
        }

    def _profile(self, profile_id: int | None) -> tuple[str | None, int | None]:
        if profile_id is None:
            return None, None
        name, target = self._profiles.get(profile_id, (None, None))
        return name, (target or None)

    def grab(self, release: dict) -> None:
        self.client.post(
            "/api/v3/release",
            {"guid": release["guid"], "indexerId": release.get("indexerId")},
        )

    # Subclasses implement the rest.
    def list_items(self) -> list[dict]:
        raise NotImplementedError

    def queue(self) -> tuple[int, set[int]]:
        raise NotImplementedError

    def item_id(self, item: dict) -> int:
        raise NotImplementedError

    def label(self, item: dict) -> str:
        raise NotImplementedError

    def runtime_h(self, item: dict) -> float:
        raise NotImplementedError

    def profile_for(self, item: dict) -> tuple[str | None, int | None]:
        raise NotImplementedError

    def current_file(self, item: dict) -> dict | None:
        raise NotImplementedError

    def current_file_id(self, item: dict) -> int | None:
        raise NotImplementedError

    def releases(self, item: dict) -> list[dict]:
        raise NotImplementedError


def _queue_ids(client: ArrClient, id_field: str) -> tuple[int, set[int]]:
    resp = client.get("/api/v3/queue?page=1&pageSize=1000") or {}
    records = resp.get("records", [])
    count = resp.get("totalRecords", len(records))
    ids = {r[id_field] for r in records if r.get(id_field)}
    return count, ids


class RadarrOptimizer(ArrOptimizer):
    app = "radarr"

    def list_items(self) -> list[dict]:
        # Select on hasFile alone, not monitored: the optimizer improves the existing
        # library, and the unmonitor feature deliberately strips monitoring once a file
        # exists — so a monitored filter would leave nothing to optimize.
        movies = self.client.get("/api/v3/movie") or []
        return [m for m in movies if m.get("hasFile")]

    def queue(self) -> tuple[int, set[int]]:
        return _queue_ids(self.client, "movieId")

    def item_id(self, item: dict) -> int:
        return item["id"]

    def label(self, item: dict) -> str:
        return f"{item.get('title')} ({item.get('year')})"

    def runtime_h(self, item: dict) -> float:
        return (item.get("runtime") or 0) / 60

    def profile_for(self, item: dict) -> tuple[str | None, int | None]:
        return self._profile(item.get("qualityProfileId"))

    def current_file_id(self, item: dict) -> int | None:
        return (item.get("movieFile") or {}).get("id")

    def current_file(self, item: dict) -> dict | None:
        file_id = self.current_file_id(item)
        if not file_id:
            return None
        return self.client.get(f"/api/v3/movieFile/{file_id}")

    def releases(self, item: dict) -> list[dict]:
        return self.client.get(f"/api/v3/release?movieId={item['id']}") or []


class SonarrOptimizer(ArrOptimizer):
    app = "sonarr"

    def __init__(self, conn: Connection):
        super().__init__(conn)
        self._series_by_id: dict[int, dict] = {}

    def list_items(self) -> list[dict]:
        series_list = self.client.get("/api/v3/series") or []
        self._series_by_id = {s["id"]: s for s in series_list}
        items: list[dict] = []
        for series in series_list:
            episodes = (
                self.client.get(
                    f"/api/v3/episode?seriesId={series['id']}&includeEpisodeFile=true"
                )
                or []
            )
            items.extend(ep for ep in episodes if ep.get("hasFile"))
        return items

    def queue(self) -> tuple[int, set[int]]:
        return _queue_ids(self.client, "episodeId")

    def item_id(self, item: dict) -> int:
        return item["id"]

    def label(self, item: dict) -> str:
        series = self._series_by_id.get(item.get("seriesId") or 0, {})
        title = series.get("title", "?")
        return f"{title} S{item.get('seasonNumber', 0):02d}E{item.get('episodeNumber', 0):02d}"

    def runtime_h(self, item: dict) -> float:
        series = self._series_by_id.get(item.get("seriesId") or 0, {})
        return (series.get("runtime") or 0) / 60

    def profile_for(self, item: dict) -> tuple[str | None, int | None]:
        series = self._series_by_id.get(item.get("seriesId") or 0, {})
        return self._profile(series.get("qualityProfileId"))

    def current_file_id(self, item: dict) -> int | None:
        return (item.get("episodeFile") or {}).get("id") or item.get("episodeFileId")

    def current_file(self, item: dict) -> dict | None:
        file_id = self.current_file_id(item)
        if not file_id:
            return None
        return self.client.get(f"/api/v3/episodefile/{file_id}")

    def releases(self, item: dict) -> list[dict]:
        return self.client.get(f"/api/v3/release?episodeId={item['id']}") or []


def build_adapter(app: str, conn: Connection) -> ArrOptimizer:
    return RadarrOptimizer(conn) if app == "radarr" else SonarrOptimizer(conn)


# ===== Worker =====


class _AppContext:
    """Per-app worker state: cached item list, active pool, last refresh time."""

    def __init__(self, adapter: ArrOptimizer):
        self.adapter = adapter
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
        self.contexts: dict[str, _AppContext] = {
            app: _AppContext(build_adapter(app, conns[app]))
            for app in self.opt.apps
            if conns[app] is not None
        }

    def stop(self) -> None:
        self._stop.set()

    # ----- per-app machinery -----

    def _refresh(self, ctx: _AppContext, now: datetime) -> None:
        adapter = ctx.adapter
        adapter.refresh_profiles()
        items = adapter.list_items()
        ctx.items_by_id = {adapter.item_id(it): it for it in items}
        ctx.evaluated.clear()
        ctx.last_refresh = now
        logger.info("[%s] list refreshed: %d items with files", adapter.app, len(items))

    def _reconcile_in_flight(self, ctx: _AppContext, queue_ids: set[int]) -> None:
        """A grabbed item that left the queue either succeeded (file id changed ->
        satisfied) or failed (file unchanged -> unprocessed, to retry next pass)."""
        adapter = ctx.adapter
        for item_id in self.state.in_flight_ids(adapter.app):
            if item_id in queue_ids:
                continue  # still downloading
            entry = self.state.get(adapter.app, item_id)
            item = ctx.items_by_id.get(item_id)
            current_fid = adapter.current_file_id(item) if item else None
            if entry and current_fid != entry.file_id_at_grab:
                logger.info("[%s] grab succeeded for id=%d -> satisfied", adapter.app, item_id)
                self.state.mark_satisfied(adapter.app, item_id, current_fid)
            else:
                logger.info("[%s] grab failed for id=%d -> retry later", adapter.app, item_id)
                self.state.clear(adapter.app, item_id)

    def _build_pool(self, ctx: _AppContext, now: datetime) -> None:
        days = self.opt.reevaluate_after_days
        ctx.pool = [
            item_id
            for item_id in ctx.items_by_id
            if item_id not in ctx.evaluated
            and self.state.is_active(ctx.adapter.app, item_id, now, days)
        ]
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

        decision = decide(self.topsis, releases, runtime_h, profile_name, target_res, current_file)
        label = adapter.label(item)

        if decision.action == "HOLD":
            logger.info("[%s] HOLD %s — %s", adapter.app, label, decision.reason)
            if not self.dry_run:
                self.state.mark_satisfied(adapter.app, item_id, adapter.current_file_id(item))
            return

        release = decision.release or {}
        title = release.get("title", "?")
        if self.dry_run:
            logger.info("[%s] would GRAB for %s: %s", adapter.app, label, title)
            return

        logger.info("[%s] GRAB for %s: %s", adapter.app, label, title)
        adapter.grab(release)
        self.state.mark_in_flight(
            adapter.app, item_id, release.get("guid", ""), adapter.current_file_id(item)
        )

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
                # Nothing actionable anywhere: wait for a queue slot or list refresh.
                self._sleep(min(self.opt.queue_recheck_seconds, self.opt.list_refresh_minutes * 60))

    def _process_app_once(self, ctx: _AppContext) -> bool:
        """Do at most one unit of work for an app. Returns True if an item was processed."""
        now = datetime.now(UTC)
        adapter = ctx.adapter

        if ctx.needs_refresh(now, self.opt.list_refresh_minutes):
            self._refresh(ctx, now)
            ctx.pool = []  # force rebuild below

        # Queue: one fetch serves the global gate and the per-item in-flight set.
        queue_count, queue_ids = adapter.queue()
        self._reconcile_in_flight(ctx, queue_ids)

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
            return False  # already in flight; drop and move on

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
