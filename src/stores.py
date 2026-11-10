"""Persistencia en Azure Table Storage para áreas, canales y correlaciones."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from areas import AREA_PARTITION_KEY, Area, AreaValidationError, area_from_entity, area_to_entity

logger = logging.getLogger("teams-adapter.stores")

CORRELATION_PARTITION_KEY = "TeamsConversation"
CHANNEL_PARTITION_KEY = "TeamsChannel"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AreaStore:
    """Catálogo de áreas con caché corta para no leer la tabla por mensaje."""

    def __init__(self, table_client, cache_seconds: int = 60):
        self._table = table_client
        self._cache_seconds = cache_seconds
        self._cache: list[Area] | None = None
        self._cache_at = 0.0

    def invalidate(self) -> None:
        self._cache = None

    async def list_areas(self, use_cache: bool = True) -> list[Area]:
        if (
            use_cache
            and self._cache is not None
            and time.monotonic() - self._cache_at < self._cache_seconds
        ):
            return self._cache

        areas: list[Area] = []
        async for entity in self._table.query_entities(
            query_filter="PartitionKey eq @pk",
            parameters={"pk": AREA_PARTITION_KEY},
        ):
            try:
                areas.append(area_from_entity(entity))
            except AreaValidationError:
                # Una fila corrupta no habilita nada: se omite y se registra.
                logger.error("Área omitida por datos inválidos | area=%s", entity.get("RowKey"))

        self._cache = areas
        self._cache_at = time.monotonic()
        return areas

    async def get_area(self, area_id: str) -> Area | None:
        try:
            entity = await self._table.get_entity(
                partition_key=AREA_PARTITION_KEY, row_key=area_id
            )
        except ResourceNotFoundError:
            return None
        return area_from_entity(entity)

    async def save_area(self, area: Area) -> None:
        await self._table.upsert_entity(entity=area_to_entity(area))
        self.invalidate()


@dataclass(frozen=True)
class KnownChannel:
    conversation_id: str
    channel_name: str
    team_id: str
    team_name: str
    team_aad_group_id: str
    service_url: str
    last_seen_at: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "conversationId": self.conversation_id,
            "channelName": self.channel_name,
            "teamId": self.team_id,
            "teamName": self.team_name,
            "teamAadGroupId": self.team_aad_group_id,
            "lastSeenAt": self.last_seen_at,
        }


def _channel_row_key(conversation_id: str) -> str:
    # RowKey no admite '/', '\\', '#', '?'. Los IDs de canal de Teams
    # no los usan, pero se sanea por seguridad.
    return "".join("_" if ch in "/\\#?" else ch for ch in conversation_id)


class KnownChannelStore:
    """Canales donde el bot ha recibido mensajes.

    Permite al administrador elegir canales reales (con el bot instalado)
    sin copiar identificadores a mano.
    """

    def __init__(self, table_client):
        self._table = table_client

    async def record(self, channel: KnownChannel) -> None:
        await self._table.upsert_entity(
            entity={
                "PartitionKey": CHANNEL_PARTITION_KEY,
                "RowKey": _channel_row_key(channel.conversation_id),
                "conversation_id": channel.conversation_id,
                "channel_name": channel.channel_name,
                "team_id": channel.team_id,
                "team_name": channel.team_name,
                "team_aad_group_id": channel.team_aad_group_id,
                "service_url": channel.service_url,
                "last_seen_at": channel.last_seen_at,
            }
        )

    async def list_channels(self) -> list[KnownChannel]:
        result: list[KnownChannel] = []
        async for e in self._table.query_entities(
            query_filter="PartitionKey eq @pk",
            parameters={"pk": CHANNEL_PARTITION_KEY},
        ):
            result.append(
                KnownChannel(
                    conversation_id=str(e.get("conversation_id", "")),
                    channel_name=str(e.get("channel_name", "")),
                    team_id=str(e.get("team_id", "")),
                    team_name=str(e.get("team_name", "")),
                    team_aad_group_id=str(e.get("team_aad_group_id", "")),
                    service_url=str(e.get("service_url", "")),
                    last_seen_at=str(e.get("last_seen_at", "")),
                )
            )
        return result

    async def exists(self, conversation_id: str) -> bool:
        try:
            await self._table.get_entity(
                partition_key=CHANNEL_PARTITION_KEY,
                row_key=_channel_row_key(conversation_id),
            )
            return True
        except ResourceNotFoundError:
            return False


@dataclass(frozen=True)
class Correlation:
    correlation_id: str
    conversation_id: str
    area_id: str
    channel_conversation_id: str
    aad_object_id: str
    expires_at: datetime | None
    responded_at: str

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or utc_now()) >= self.expires_at


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class CorrelationStore:
    def __init__(self, table_client, ttl_hours: int = 24):
        self._table = table_client
        self._ttl = timedelta(hours=ttl_hours)

    async def save(
        self,
        correlation_id: str,
        conversation: dict[str, str],
        area_id: str = "",
        channel_conversation_id: str = "",
    ) -> None:
        now = utc_now()
        await self._table.upsert_entity(
            entity={
                "PartitionKey": CORRELATION_PARTITION_KEY,
                "RowKey": correlation_id,
                "service_url": conversation.get("service_url", ""),
                "conversation_id": conversation.get("conversation_id", ""),
                "tenant_id": conversation.get("tenant_id", ""),
                "user_id": conversation.get("user_id", ""),
                "aad_object_id": conversation.get("aad_object_id", ""),
                "bot_id": conversation.get("bot_id", ""),
                "channel_id": conversation.get("channel_id", ""),
                "activity_id": conversation.get("activity_id", ""),
                "area_id": area_id,
                "channel_conversation_id": channel_conversation_id,
                "created_at": now.isoformat(),
                "expires_at": (now + self._ttl).isoformat() if area_id else "",
                "responded_at": "",
            }
        )

    async def get(self, correlation_id: str) -> Correlation | None:
        try:
            e = await self._table.get_entity(
                partition_key=CORRELATION_PARTITION_KEY, row_key=correlation_id
            )
        except ResourceNotFoundError:
            return None
        return Correlation(
            correlation_id=correlation_id,
            conversation_id=str(e.get("conversation_id", "")),
            area_id=str(e.get("area_id", "")),
            channel_conversation_id=str(e.get("channel_conversation_id", "")),
            aad_object_id=str(e.get("aad_object_id", "")),
            expires_at=_parse_datetime(e.get("expires_at")),
            responded_at=str(e.get("responded_at", "")),
        )

    async def mark_responded(self, correlation_id: str) -> None:
        await self._table.update_entity(
            entity={
                "PartitionKey": CORRELATION_PARTITION_KEY,
                "RowKey": correlation_id,
                "responded_at": utc_now().isoformat(),
            },
            mode="merge",
        )

    async def delete(self, correlation_id: str) -> None:
        await self._table.delete_entity(
            partition_key=CORRELATION_PARTITION_KEY, row_key=correlation_id
        )
