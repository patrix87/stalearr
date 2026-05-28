"""Per-item optimizer state, persisted to JSON.

State is keyed by app ("radarr"/"sonarr") then item id (movie id / episode id).
The lifecycle is what makes failed downloads self-correcting (no cooldown timer):

  unprocessed  -> not in state
  satisfied    -> HOLD: nothing better than the current file (record file_id + ts)
  in_flight    -> ACT: a grab was posted (record guid + file_id_at_grab + ts)

Reconciliation (worker, once a grabbed item leaves the queue):
  in_flight -> satisfied   if the file id changed   (grab succeeded)
  in_flight -> unprocessed if the file id is the same (grab failed; Radarr blocklists it)
  satisfied -> unprocessed once reevaluate_after_days has elapsed
"""

import json
import logging
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from optimizarr.dates import parse_iso

logger = logging.getLogger("optimizarr")

SATISFIED = "satisfied"
IN_FLIGHT = "in_flight"


@dataclass
class StateEntry:
    status: str
    updated_at: str
    file_id: int | None = None
    guid: str | None = None
    file_id_at_grab: int | None = None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class StateManager:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict[str, StateEntry]] = {"radarr": {}, "sonarr": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                raw = json.load(f)
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("[state] could not read %s (%s); starting empty", self.path, e)
            return
        for app, items in raw.items():
            bucket = self._data.setdefault(app, {})
            for item_id, entry in items.items():
                bucket[str(item_id)] = StateEntry(**entry)

    def _save_locked(self) -> None:
        serializable = {
            app: {item_id: asdict(entry) for item_id, entry in items.items()}
            for app, items in self._data.items()
        }
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(serializable, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    # ----- reads -----

    def get(self, app: str, item_id: int) -> StateEntry | None:
        return self._data.get(app, {}).get(str(item_id))

    def in_flight_ids(self, app: str) -> set[int]:
        return {
            int(item_id)
            for item_id, entry in self._data.get(app, {}).items()
            if entry.status == IN_FLIGHT
        }

    def is_active(self, app: str, item_id: int, now: datetime, reevaluate_after_days: int) -> bool:
        """An item is active (worth picking) unless it's in flight or satisfied within
        the reevaluate window. Expired satisfied entries are active again."""
        entry = self.get(app, item_id)
        if entry is None:
            return True
        if entry.status == IN_FLIGHT:
            return False
        if entry.status == SATISFIED:
            ts = parse_iso(entry.updated_at)
            if ts is None:
                return True
            age_days = (now - ts).total_seconds() / 86400
            return age_days >= reevaluate_after_days
        return True

    # ----- writes -----

    def mark_satisfied(self, app: str, item_id: int, file_id: int | None) -> None:
        with self._lock:
            self._data.setdefault(app, {})[str(item_id)] = StateEntry(
                status=SATISFIED, updated_at=_now_iso(), file_id=file_id
            )
            self._save_locked()

    def mark_in_flight(
        self, app: str, item_id: int, guid: str, file_id_at_grab: int | None
    ) -> None:
        with self._lock:
            self._data.setdefault(app, {})[str(item_id)] = StateEntry(
                status=IN_FLIGHT,
                updated_at=_now_iso(),
                guid=guid,
                file_id_at_grab=file_id_at_grab,
            )
            self._save_locked()

    def clear(self, app: str, item_id: int) -> None:
        """Reset an item to unprocessed (e.g. a failed grab that must be retried)."""
        with self._lock:
            self._data.get(app, {}).pop(str(item_id), None)
            self._save_locked()
