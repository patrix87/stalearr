"""Per-app Radarr/Sonarr API clients (the layer features are built on)."""

from optimizarr.arr.base import ArrApi, max_allowed_resolution
from optimizarr.arr.radarr import RadarrApi
from optimizarr.arr.sonarr import SonarrApi
from optimizarr.config import Connection

__all__ = ["ArrApi", "RadarrApi", "SonarrApi", "build_client", "max_allowed_resolution"]


def build_client(app: str, conn: Connection) -> ArrApi:
    return RadarrApi(conn) if app == "radarr" else SonarrApi(conn)
