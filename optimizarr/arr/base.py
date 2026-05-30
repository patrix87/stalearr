"""ArrApi: the per-app Radarr/Sonarr API surface that features depend on.

This is the abstraction (DIP): features (optimizer, unmonitor) talk to ArrApi methods,
never to raw `/api/v3/...` endpoints or to the concrete RadarrApi/SonarrApi. It owns all
knowledge of how to talk to a *arr app — listing items, fetching files/releases, grabbing,
reading the queue, and setting monitored state — plus normalized accessors so feature
logic can be written once for both movies and episodes.

Item *selection* (age gate, monitored, cutoff) lives in the features, not here: this layer
lists raw items and exposes accessors; each feature applies its own predicate.
"""

from urllib.parse import quote

from optimizarr.config import Connection
from optimizarr.http import ArrClient

# trackedDownloadState values that mean the download finished and is no longer consuming
# bandwidth — used to filter "active" queue items vs. ones stuck waiting for/in import.
QUEUE_INACTIVE_STATES = {"importPending", "importing", "imported", "importBlocked"}


def max_allowed_resolution(profile_items: list[dict]) -> int:
    """Max `resolution` over allowed entries in a Radarr/Sonarr quality profile's
    items[] (with nested items[]). Returns 0 if nothing is allowed."""
    best = 0
    for it in profile_items or []:
        q = it.get("quality") or {}
        if it.get("allowed") and q.get("resolution"):
            best = max(best, q["resolution"])
        for sub in it.get("items") or []:
            sq = sub.get("quality") or {}
            if sub.get("allowed") and sq.get("resolution"):
                best = max(best, sq["resolution"])
    return best


class ArrApi:
    app: str
    _queue_id_field: str  # "movieId" / "episodeId"

    def __init__(self, conn: Connection):
        self.client = ArrClient(conn.url, conn.api_key)
        self._profiles: dict[int, tuple[str, int]] = {}

    # ----- quality profiles -----

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

    # ----- normalized accessors (concrete; share across apps) -----

    def item_id(self, item: dict) -> int:
        return item["id"]

    def monitored(self, item: dict) -> bool:
        return item.get("monitored", False)

    def has_file(self, item: dict) -> bool:
        return item.get("hasFile", False)

    def reference_date(self, item: dict, release_type: str) -> str | None:
        if release_type == "dateAdded":
            return (self._embedded_file(item) or {}).get("dateAdded")
        return item.get(release_type)

    def cutoff_met(self, item: dict) -> bool:
        return not (self._embedded_file(item) or {}).get("qualityCutoffNotMet", True)

    def current_file_id(self, item: dict) -> int | None:
        return (self._embedded_file(item) or {}).get("id")

    # ----- actions (concrete) -----

    def grab(self, release: dict) -> None:
        self.client.post(
            "/api/v3/release",
            {"guid": release["guid"], "indexerId": release.get("indexerId")},
        )

    def queue_items(self) -> list[dict]:
        """Full queue records (status, trackedDownloadState, statusMessages, etc.) — the
        worker computes both the queue_max count and the in-progress item-id set from this."""
        resp = self.client.get("/api/v3/queue?page=1&pageSize=1000") or {}
        return resp.get("records", []) or []

    def queue_item_id(self, record: dict) -> int | None:
        """movieId / episodeId for a queue record (None if missing)."""
        return record.get(self._queue_id_field)

    @staticmethod
    def is_queue_item_active(record: dict) -> bool:
        """True when the record represents an item still downloading or queued — i.e. NOT
        completed and waiting for / already past import. Used by `ignore_completed_in_queue`."""
        if (record.get("status") or "").lower() == "completed":
            return False
        return record.get("trackedDownloadState") not in QUEUE_INACTIVE_STATES

    def manual_import_candidates(self, download_id: str) -> list[dict]:
        """List importable files for a downloadId (GET /api/v3/manualimport?downloadId=).
        Each candidate carries proposed movie/episode, quality, customFormats, rejections."""
        return self.client.get(f"/api/v3/manualimport?downloadId={quote(download_id)}") or []

    def manual_import(self, items: list[dict], import_mode: str = "auto") -> None:
        """POST /api/v3/manualimport with the given candidate items. Same path on both apps."""
        body = [{**it, "importMode": import_mode} for it in items]
        self.client.post("/api/v3/manualimport", body)

    # ----- app-specific (subclasses implement) -----

    def _embedded_file(self, item: dict) -> dict | None:
        """The file object carried on the item itself (movieFile / episodeFile)."""
        raise NotImplementedError

    def list_items(self) -> list[dict]:
        raise NotImplementedError

    def label(self, item: dict) -> str:
        raise NotImplementedError

    def runtime_h(self, item: dict) -> float:
        raise NotImplementedError

    def profile_for(self, item: dict) -> tuple[str | None, int | None]:
        raise NotImplementedError

    def current_file(self, item: dict) -> dict | None:
        raise NotImplementedError

    def releases(self, item: dict) -> list[dict]:
        raise NotImplementedError

    def set_monitored(self, item_ids: list[int], monitored: bool) -> None:
        raise NotImplementedError
