"""The unmonitor business rule: should this item be unmonitored?

App-agnostic — reads everything through the ArrApi accessors, so the same rule applies to
movies and episodes. Never unmonitors wanted-but-undownloaded items (that would mean
"give up on this"); only items that already have a file and are old enough.
"""

from datetime import datetime

from optimizarr.arr import ArrApi
from optimizarr.dates import age_days
from optimizarr.features.unmonitor.config import UnmonitorAppConfig


def is_candidate(
    api: ArrApi, item: dict, cfg: UnmonitorAppConfig, now: datetime
) -> tuple[bool, str]:
    """Return (should_unmonitor, reason)."""
    if not api.monitored(item):
        return False, "not monitored"

    if not api.has_file(item):
        return False, "no file"

    if cfg.require_cutoff_met and not api.cutoff_met(item):
        return False, "quality cutoff not met"

    age = age_days(api.reference_date(item, cfg.release_type), now)
    if age is None:
        return False, f"no {cfg.release_type} date"
    if age < cfg.days:
        return False, f"only {age:.1f}d since {cfg.release_type}"

    return True, f"{age:.1f}d since {cfg.release_type}"
