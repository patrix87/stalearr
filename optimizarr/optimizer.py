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
in-flight bookkeeping and a restart needs no reconciliation. App-specific HTTP lives in the
RadarrOptimizer / SonarrOptimizer adapters; the worker loop and per-item decision are
app-agnostic.
"""

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from optimizarr.config import Config, Connection, OptimizerAppConfig, OptimizerConfig
from optimizarr.dates import age_days
from optimizarr.http import ArrClient
from optimizarr.state import StateManager
from optimizarr.topsis import Topsis, max_allowed_resolution, winning_path

logger = logging.getLogger("optimizarr")

_PATH_LABELS = {
    "path_a": "shrink (smaller at equal quality)",
    "path_b": "upgrade (better quality)",
}


@dataclass
class Decision:
    action: str  # "ACT" or "HOLD"
    reason: str
    profile_name: str | None = None
    current: dict | None = None  # {score, resolution, gbh, size_gb, closeness}
    pick: dict | None = None  # {score, resolution, gbh, size_gb, closeness, title}
    path: str | None = None  # winning gate path for ACT ("path_a"/"path_b")
    gates: list | None = None
    release: dict | None = None  # raw release to grab (ACT only)
    diag: dict | None = None


def decide(
    topsis: Topsis,
    releases: list[dict],
    runtime_h: float,
    profile_name: str | None,
    target_resolution: int | None,
    current_file: dict | None,
) -> Decision:
    """Pure decision: given fetched data, return ACT (with the release) or HOLD,
    carrying the current-vs-pick details that the worker logs."""
    current_file_score = (current_file or {}).get("customFormatScore")
    pick, diag = topsis.pick(
        releases, runtime_h, profile_name, target_resolution, current_file_score
    )
    current_closeness, cur_raw = topsis.closeness_for_current_file(
        current_file or {}, runtime_h, profile_name, target_resolution
    )
    current = {"closeness": current_closeness, **cur_raw}

    if pick is None:
        return Decision(
            "HOLD",
            f"no viable candidate ({diag['score_floor_tier']})",
            profile_name=profile_name,
            current=current,
            diag=diag,
        )

    release, attrs, pick_closeness = pick
    raw = attrs["raw"]
    pick_info = {"closeness": pick_closeness, "title": release.get("title", "?"), **raw}

    pick_size = release.get("size", 0)
    current_size = (current_file or {}).get("size", 0) or 0
    gates = topsis.evaluate_gates(pick_closeness, pick_size, current_closeness, current_size)
    path = winning_path(gates)

    if path is not None:
        return Decision(
            "ACT",
            _PATH_LABELS.get(path, path),
            profile_name=profile_name,
            current=current,
            pick=pick_info,
            path=path,
            gates=gates,
            release=release,
            diag=diag,
        )
    return Decision(
        "HOLD",
        "nothing better than current file",
        profile_name=profile_name,
        current=current,
        pick=pick_info,
        gates=gates,
        diag=diag,
    )


def _fmt_side(side: dict | None) -> str:
    if not side:
        return "n/a"
    score = side.get("score")
    score_s = f"{score:,}" if score is not None else "n/a"
    clo = side.get("closeness")
    clo_s = f"{clo:.3f}" if clo is not None else "n/a"
    res = side.get("resolution") or 0
    res_s = f"{res}p" if res else "?"
    return (
        f"score={score_s} res={res_s} size={side.get('size_gb', 0):.1f}GB "
        f"({side.get('gbh', 0):.1f} GB/h) closeness={clo_s}"
    )


def _fmt_deltas(current: dict | None, pick: dict | None) -> str:
    if not current or not pick:
        return ""
    parts = []
    c_clo, p_clo = current.get("closeness"), pick.get("closeness")
    if c_clo is not None and p_clo is not None:
        parts.append(f"Δcloseness {p_clo - c_clo:+.3f}")
    parts.append(f"Δsize {pick.get('size_gb', 0) - current.get('size_gb', 0):+.1f}GB")
    return "  (" + ", ".join(parts) + ")" if parts else ""


def format_decision(app: str, label: str, decision: Decision, dry_run: bool) -> str:
    """Multi-line, human-readable explanation of one decision (current vs pick)."""
    profile = decision.profile_name or "?"
    if decision.action == "ACT":
        verb = "would GRAB" if dry_run else "GRAB"
        head = (
            f"[{app}] {verb} — {label}  [profile={profile}]  via {decision.path} {decision.reason}"
        )
    else:
        head = f"[{app}] HOLD — {label}  [profile={profile}]  ({decision.reason})"

    lines = [head, f"    current: {_fmt_side(decision.current)}"]
    if decision.pick:
        candidate_label = "pick" if decision.action == "ACT" else "best   "
        lines.append(
            f"    {candidate_label}: {_fmt_side(decision.pick)}"
            f"{_fmt_deltas(decision.current, decision.pick)}"
        )
        lines.append(f"    release: {decision.pick.get('title', '?')}")
    if decision.action == "HOLD" and decision.gates:
        failed = [f"{n} [{d}]" for n, ok, d in decision.gates if ok is not True]
        if failed:
            lines.append(f"    gates not met: {'; '.join(failed)}")
    return "\n".join(lines)


# ===== App adapters =====


class ArrOptimizer:
    """Base adapter. Subclasses wire the Radarr/Sonarr endpoints."""

    app: str

    def __init__(self, conn: Connection, app_cfg: OptimizerAppConfig):
        self.client = ArrClient(conn.url, conn.api_key)
        self.min_age_days = app_cfg.min_age_days
        self.release_type = app_cfg.release_type
        self._profiles: dict[int, tuple[str, int]] = {}

    def age_ok(self, item: dict, now: datetime) -> bool:
        """True if the item is old enough to consider. With min_age_days <= 0 there is
        no gate. When gating is on and the release date is unknown, the item is skipped."""
        if self.min_age_days <= 0:
            return True
        age = age_days(self.reference_date(item), now)
        return age is not None and age >= self.min_age_days

    def reference_date(self, item: dict) -> str | None:
        raise NotImplementedError

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
    def list_items(self, now: datetime) -> list[dict]:
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

    def list_items(self, now: datetime) -> list[dict]:
        # Select on hasFile alone, not monitored: the optimizer improves the existing
        # library, and the unmonitor feature deliberately strips monitoring once a file
        # exists — so a monitored filter would leave nothing to optimize.
        movies = self.client.get("/api/v3/movie") or []
        return [m for m in movies if m.get("hasFile") and self.age_ok(m, now)]

    def reference_date(self, item: dict) -> str | None:
        if self.release_type == "dateAdded":
            return (item.get("movieFile") or {}).get("dateAdded")
        return item.get(self.release_type)

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

    def __init__(self, conn: Connection, app_cfg: OptimizerAppConfig):
        super().__init__(conn, app_cfg)
        self._series_by_id: dict[int, dict] = {}

    def list_items(self, now: datetime) -> list[dict]:
        series_list = self.client.get("/api/v3/series") or []
        self._series_by_id = {s["id"]: s for s in series_list}
        items: list[dict] = []
        for series in series_list:
            episodes = (
                self.client.get(f"/api/v3/episode?seriesId={series['id']}&includeEpisodeFile=true")
                or []
            )
            items.extend(ep for ep in episodes if ep.get("hasFile") and self.age_ok(ep, now))
        return items

    def reference_date(self, item: dict) -> str | None:
        if self.release_type == "dateAdded":
            return (item.get("episodeFile") or {}).get("dateAdded")
        return item.get(self.release_type)

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


def build_adapter(app: str, conn: Connection, app_cfg: OptimizerAppConfig) -> ArrOptimizer:
    cls = RadarrOptimizer if app == "radarr" else SonarrOptimizer
    return cls(conn, app_cfg)


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
        app_cfgs = {"radarr": self.opt.radarr, "sonarr": self.opt.sonarr}
        self.contexts: dict[str, _AppContext] = {}
        for app in self.opt.apps:
            conn = conns[app]
            if conn is None:
                continue
            self.contexts[app] = _AppContext(build_adapter(app, conn, app_cfgs[app]))

    def stop(self) -> None:
        self._stop.set()

    # ----- per-app machinery -----

    def _refresh(self, ctx: _AppContext, now: datetime) -> None:
        adapter = ctx.adapter
        adapter.refresh_profiles()
        items = adapter.list_items(now)
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

        decision = decide(self.topsis, releases, runtime_h, profile_name, target_res, current_file)
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
