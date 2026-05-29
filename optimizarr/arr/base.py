"""ArrApi: the per-app Radarr/Sonarr API surface that features depend on.

This is the abstraction (DIP): features (optimizer, unmonitor) talk to ArrApi methods,
never to raw `/api/v3/...` endpoints or to the concrete RadarrApi/SonarrApi. It owns all
knowledge of how to talk to a *arr app — listing items, fetching files/releases, grabbing,
reading the queue, and setting monitored state — plus normalized accessors so feature
logic can be written once for both movies and episodes.

Item *selection* (age gate, monitored, cutoff) lives in the features, not here: this layer
lists raw items and exposes accessors; each feature applies its own predicate.
"""

from optimizarr.config import Connection
from optimizarr.http import ArrClient


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

    def queue(self) -> tuple[int, set[int]]:
        resp = self.client.get("/api/v3/queue?page=1&pageSize=1000") or {}
        records = resp.get("records", [])
        count = resp.get("totalRecords", len(records))
        ids = {r[self._queue_id_field] for r in records if r.get(self._queue_id_field)}
        return count, ids

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
