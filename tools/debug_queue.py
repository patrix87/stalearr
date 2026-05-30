"""Live Radarr/Sonarr queue probe — dumps the raw shape of records that look like
stuck imports, plus the manualimport candidates the worker would consider.

Usage (from repo root):
    set -a; source .env; set +a; uv run python tools/debug_queue.py [radarr|sonarr]

This is a READ-ONLY probe — it never POSTs. The goal is to verify whether the
worker's assumptions about status / trackedDownloadState / statusMessages phrasing
and the manualimport candidate `rejections` shape match what the live API returns.
"""

from __future__ import annotations

import json
import os
import sys
from urllib.parse import quote

from optimizarr.http import ArrClient


def _redact(url: str | None) -> str:
    if not url:
        return "<missing>"
    # keep the host visible but hide the port if it's a homelab port; not important —
    # we just don't want full URLs landing in logs the user shares.
    return url


def _probe(app: str, base_url: str, api_key: str) -> None:
    print(f"\n===== {app.upper()} @ {_redact(base_url)} =====")
    client = ArrClient(base_url, api_key)

    queue = client.get("/api/v3/queue?page=1&pageSize=1000") or {}
    records = queue.get("records") or []
    print(f"queue total={queue.get('totalRecords', len(records))} records_returned={len(records)}")

    # Tally what we see so the user gets a quick picture even if nothing matches our
    # strict criteria.
    by_status: dict[str, int] = {}
    by_state: dict[str, int] = {}
    for r in records:
        by_status[(r.get("status") or "").lower()] = (
            by_status.get((r.get("status") or "").lower(), 0) + 1
        )
        by_state[r.get("trackedDownloadState") or ""] = (
            by_state.get(r.get("trackedDownloadState") or "", 0) + 1
        )
    print(f"by status:               {by_status}")
    print(f"by trackedDownloadState: {by_state}")

    # Show every record that has ANY statusMessages — that's where the import rejection
    # text lives, and it's the surface our worker greps for "Not an upgrade".
    print("\n--- records with statusMessages ---")
    interesting = [r for r in records if r.get("statusMessages")]
    print(f"count={len(interesting)}")
    for r in interesting[:10]:
        print(
            json.dumps(
                {
                    "id": r.get("id"),
                    "movieId": r.get("movieId"),
                    "episodeId": r.get("episodeId"),
                    "downloadId": r.get("downloadId"),
                    "title": r.get("title"),
                    "status": r.get("status"),
                    "trackedDownloadStatus": r.get("trackedDownloadStatus"),
                    "trackedDownloadState": r.get("trackedDownloadState"),
                    "statusMessages": r.get("statusMessages"),
                    "errorMessage": r.get("errorMessage"),
                },
                indent=2,
                default=str,
            )
        )

    # Pull manualimport candidates for every distinct downloadId seen with messages.
    print("\n--- manualimport candidates per downloadId ---")
    seen: set[str] = set()
    for r in interesting:
        dlid = r.get("downloadId")
        if not dlid or dlid in seen:
            continue
        seen.add(dlid)
        try:
            cands = client.get(f"/api/v3/manualimport?downloadId={quote(dlid)}") or []
        except Exception as e:
            print(f"  downloadId={dlid}: GET failed: {e}")
            continue
        print(f"  downloadId={dlid}: {len(cands)} candidate(s)")
        for c in cands[:5]:
            print(
                json.dumps(
                    {
                        "path": c.get("path"),
                        "movie": (c.get("movie") or {}).get("title"),
                        "series": (c.get("series") or {}).get("title"),
                        "episodes_n": len(c.get("episodes") or []),
                        "quality": c.get("quality"),
                        "customFormatScore": c.get("customFormatScore"),
                        "rejections": c.get("rejections"),
                    },
                    indent=2,
                    default=str,
                )
            )


def main() -> None:
    apps = sys.argv[1:] or ["radarr", "sonarr"]
    for app in apps:
        prefix = app.upper()
        url = (os.environ.get(f"{prefix}_URL") or "").rstrip("/")
        key = os.environ.get(f"{prefix}_API_KEY") or ""
        if not url or not key:
            print(f"{app}: missing {prefix}_URL / {prefix}_API_KEY in env; skipping")
            continue
        try:
            _probe(app, url, key)
        except Exception as e:
            print(f"{app}: probe failed: {e}")


if __name__ == "__main__":
    main()
