"""Nombres de equipo y canal para el catálogo de canales conocidos.

Los mensajes de canal de Teams sólo traen IDs. Los nombres se consultan
con la API de Teams del bot (``/v3/teams/{id}`` y
``/v3/teams/{id}/conversations``) y se guardan en caché por equipo.
Un fallo nunca bloquea el mensaje: se devuelven nombres vacíos.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger("teams-adapter.channel-names")

GENERAL_CHANNEL_NAME = "General"


@dataclass(frozen=True)
class TeamNames:
    team_name: str = ""
    channels: dict[str, str] = field(default_factory=dict)

    def channel_name(self, team_id: str, channel_id: str) -> str:
        name = self.channels.get(channel_id, "")
        if not name and channel_id and channel_id == team_id:
            # El canal General comparte ID con el equipo y llega sin nombre.
            return GENERAL_CHANNEL_NAME
        return name


# fetch(team_id, service_url) -> TeamNames
Fetcher = Callable[[str, str], Awaitable[TeamNames]]


class ChannelNameResolver:
    def __init__(self, fetch: Fetcher, cache_seconds: int = 600):
        self._fetch = fetch
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, TeamNames]] = {}

    async def names(
        self, team_id: str, channel_id: str, service_url: str
    ) -> tuple[str, str]:
        """Devuelve (nombre_equipo, nombre_canal); vacíos si no se pudo."""
        if not team_id:
            return "", ""
        cached = self._cache.get(team_id)
        fresh = cached and time.monotonic() - cached[0] < self._cache_seconds
        # Un canal nuevo no está en caché: se vuelve a consultar.
        if not fresh or channel_id not in cached[1].channels and channel_id != team_id:
            try:
                team = await self._fetch(team_id, service_url)
                self._cache[team_id] = (time.monotonic(), team)
                cached = self._cache[team_id]
            except Exception as exc:  # noqa: BLE001 - nunca bloquear el mensaje
                logger.warning(
                    "No se pudieron obtener nombres de Teams | error=%s",
                    type(exc).__name__,
                )
                if not cached:
                    return "", ""
        team = cached[1]
        return team.team_name, team.channel_name(team_id, channel_id)
