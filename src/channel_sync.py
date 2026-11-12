"""Descubrimiento de equipos y canales donde está instalada la app.

Al instalar la app en un equipo (o al crear/renombrar canales), el bot
consulta la API de Teams y registra todos los canales del equipo en
TeamsKnownChannels. Así el administrador puede asignar canales a áreas sin
que nadie tenga que mencionar al bot en cada uno.

No usa Microsoft Graph: la API del bot sólo ve equipos donde la app está
instalada, que son justamente los únicos donde el bot puede publicar.
"""

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from stores import KnownChannel, utc_now

logger = logging.getLogger("teams-adapter.channels")


@dataclass(frozen=True)
class TeamSnapshot:
    team_id: str
    team_name: str
    aad_group_id: str
    channels: tuple[tuple[str, str], ...]  # (conversation_id, nombre)


# Recibe (team_id, service_url) y devuelve el estado actual del equipo.
TeamFetcher = Callable[[str, str], Awaitable[TeamSnapshot]]


@dataclass(frozen=True)
class SyncResult:
    team_id: str
    team_name: str
    recorded: int
    removed: int


class ChannelSync:
    def __init__(self, store, fetch: TeamFetcher):
        self.store = store
        self.fetch = fetch

    async def sync_team(self, team_id: str, service_url: str) -> SyncResult:
        """Registra todos los canales del equipo y elimina los que ya no
        existen. Propaga errores de la API para que el llamador decida."""
        snapshot = await self.fetch(team_id, service_url)
        now = utc_now().isoformat()
        current: set[str] = set()
        for conversation_id, name in snapshot.channels:
            if not conversation_id:
                continue
            current.add(conversation_id)
            await self.store.record(
                KnownChannel(
                    conversation_id=conversation_id,
                    # El canal General comparte el ID del equipo y a veces
                    # llega sin nombre.
                    channel_name=name or ("General" if conversation_id == team_id else ""),
                    team_id=team_id,
                    team_name=snapshot.team_name,
                    team_aad_group_id=snapshot.aad_group_id,
                    service_url=service_url,
                    last_seen_at=now,
                )
            )

        removed = 0
        for known in await self.store.list_channels():
            if known.team_id == team_id and known.conversation_id not in current:
                await self.store.delete(known.conversation_id)
                removed += 1

        logger.info(
            "Canales sincronizados | equipo=%s | canales=%s | eliminados=%s",
            snapshot.team_name or team_id, len(current), removed,
        )
        return SyncResult(team_id, snapshot.team_name, len(current), removed)

    async def sync_safely(self, team_id: str, service_url: str) -> SyncResult | None:
        """Para manejadores de eventos: nunca lanza."""
        if not team_id:
            return None
        try:
            return await self.sync_team(team_id, service_url)
        except Exception:  # noqa: BLE001
            logger.exception("No se pudieron sincronizar los canales | equipo=%s", team_id)
            return None

    async def known_teams(self) -> dict[str, str]:
        """team_id -> service_url de los equipos ya registrados."""
        teams: dict[str, str] = {}
        for channel in await self.store.list_channels():
            if channel.team_id and channel.team_id not in teams:
                teams[channel.team_id] = channel.service_url
        return teams

    async def sync_known_teams(self, team_id: str = "") -> list[SyncResult]:
        """Resincroniza los equipos ya conocidos (o sólo uno)."""
        teams = await self.known_teams()
        if team_id:
            if team_id not in teams:
                raise KeyError(team_id)
            teams = {team_id: teams[team_id]}
        results = []
        for tid, service_url in teams.items():
            results.append(await self.sync_team(tid, service_url))
        return results


def group_by_team(channels, owners: dict[str, tuple[str, str]]) -> list[dict]:
    """Agrupa canales conocidos por equipo para la API de administración.

    owners: conversation_id -> (area_id, alias)
    """
    teams: dict[str, dict] = {}
    for c in sorted(channels, key=lambda c: (c.team_name.lower(), c.channel_name.lower())):
        team = teams.setdefault(
            c.team_id,
            {"teamId": c.team_id, "teamName": c.team_name, "channels": []},
        )
        area_id, alias = owners.get(c.conversation_id, (None, None))
        team["channels"].append(
            {
                "conversationId": c.conversation_id,
                "channelName": c.channel_name,
                "areaId": area_id,
                "alias": alias,
                "lastSeenAt": c.last_seen_at,
            }
        )
    return list(teams.values())
