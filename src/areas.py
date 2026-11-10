"""Modelo y validación del catálogo de áreas de Teams.

Un área agrupa canales de Teams, define qué grupos de Entra ID deben
cumplir los usuarios (modo «any»: al menos uno; «all»: todos) y qué
agente atiende las solicitudes. Los secretos del agente se guardan en
Key Vault; el catálogo sólo almacena sus nombres.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

AREA_PARTITION_KEY = "TeamsArea"

ACCESS_MODES = ("any", "all")

AREA_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
ALIAS_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SECRET_NAME_PATTERN = re.compile(r"^[0-9A-Za-z-]{1,127}$")
CONVERSATION_ID_PATTERN = re.compile(r"^19:[^\s;]{1,250}$")

MAX_DISPLAY_NAME = 200
MAX_GROUPS = 50
MAX_CHANNELS = 50


class AreaValidationError(ValueError):
    """Datos de área inválidos. El mensaje es apto para el administrador."""


@dataclass(frozen=True)
class AreaChannel:
    alias: str
    conversation_id: str
    display_name: str = ""


@dataclass(frozen=True)
class Area:
    id: str
    display_name: str
    access_mode: str
    required_groups: tuple[str, ...]
    channels: tuple[AreaChannel, ...]
    agent_trigger_url_secret: str
    agent_token_secret: str
    enabled: bool = True
    allow_publish_without_request: bool = False
    updated_at: str = ""
    updated_by: str = ""

    def channel_by_alias(self, alias: str) -> AreaChannel | None:
        return next((c for c in self.channels if c.alias == alias), None)

    def channel_by_conversation(self, conversation_id: str) -> AreaChannel | None:
        return next(
            (c for c in self.channels if c.conversation_id == conversation_id),
            None,
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "displayName": self.display_name,
            "enabled": self.enabled,
            "accessMode": self.access_mode,
            "requiredGroups": list(self.required_groups),
            "channels": [
                {
                    "alias": c.alias,
                    "conversationId": c.conversation_id,
                    "displayName": c.display_name,
                }
                for c in self.channels
            ],
            "agent": {
                "triggerUrlSecret": self.agent_trigger_url_secret,
                "tokenSecret": self.agent_token_secret,
            },
            "allowPublishWithoutRequest": self.allow_publish_without_request,
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by,
        }


@dataclass
class AreaPayloadErrors:
    messages: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.messages.append(message)

    def raise_if_any(self) -> None:
        if self.messages:
            raise AreaValidationError("; ".join(self.messages))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_area_payload(area_id: str, payload: Any, updated_by: str = "") -> Area:
    """Valida el cuerpo recibido por el endpoint de administración."""
    errors = AreaPayloadErrors()

    if not isinstance(payload, dict):
        raise AreaValidationError("El cuerpo debe ser un objeto JSON")

    area_id = _text(area_id)
    if not AREA_ID_PATTERN.match(area_id):
        errors.add("id de área inválido: usar minúsculas, números y guiones (2-64)")

    display_name = _text(payload.get("displayName"))
    if not display_name or len(display_name) > MAX_DISPLAY_NAME:
        errors.add("displayName es obligatorio (máx. 200 caracteres)")

    access_mode = _text(payload.get("accessMode")).lower()
    if access_mode not in ACCESS_MODES:
        errors.add("accessMode debe ser 'any' o 'all'")

    raw_groups = payload.get("requiredGroups")
    groups: list[str] = []
    if not isinstance(raw_groups, list) or not raw_groups:
        errors.add("requiredGroups debe incluir al menos un grupo de Entra ID")
    elif len(raw_groups) > MAX_GROUPS:
        errors.add(f"requiredGroups admite como máximo {MAX_GROUPS} grupos")
    else:
        for value in raw_groups:
            group = _text(value).lower()
            if not GUID_PATTERN.match(group):
                errors.add(f"grupo inválido (se espera Object ID): {value!r}")
            elif group not in groups:
                groups.append(group)

    raw_channels = payload.get("channels")
    channels: list[AreaChannel] = []
    if not isinstance(raw_channels, list) or not raw_channels:
        errors.add("channels debe incluir al menos un canal")
    elif len(raw_channels) > MAX_CHANNELS:
        errors.add(f"channels admite como máximo {MAX_CHANNELS} canales")
    else:
        seen_alias: set[str] = set()
        seen_conversation: set[str] = set()
        for item in raw_channels:
            if not isinstance(item, dict):
                errors.add("cada canal debe ser un objeto")
                continue
            alias = _text(item.get("alias")).lower()
            conversation_id = _text(item.get("conversationId"))
            name = _text(item.get("displayName"))[:MAX_DISPLAY_NAME]
            if not ALIAS_PATTERN.match(alias):
                errors.add(f"alias inválido: {alias!r} (minúsculas, números y _)")
            if not CONVERSATION_ID_PATTERN.match(conversation_id):
                errors.add(f"conversationId inválido para {alias!r}")
            if alias in seen_alias:
                errors.add(f"alias repetido: {alias!r}")
            if conversation_id in seen_conversation:
                errors.add(f"canal repetido: {conversation_id!r}")
            seen_alias.add(alias)
            seen_conversation.add(conversation_id)
            channels.append(AreaChannel(alias, conversation_id, name))

    agent = payload.get("agent")
    trigger_secret = token_secret = ""
    if not isinstance(agent, dict):
        errors.add("agent es obligatorio (triggerUrlSecret y tokenSecret)")
    else:
        trigger_secret = _text(agent.get("triggerUrlSecret"))
        token_secret = _text(agent.get("tokenSecret"))
        for label, value in (
            ("agent.triggerUrlSecret", trigger_secret),
            ("agent.tokenSecret", token_secret),
        ):
            if not SECRET_NAME_PATTERN.match(value):
                errors.add(f"{label} debe ser el nombre de un secreto de Key Vault")

    enabled = payload.get("enabled", True)
    allow_without_request = payload.get("allowPublishWithoutRequest", False)
    if not isinstance(enabled, bool):
        errors.add("enabled debe ser booleano")
    if not isinstance(allow_without_request, bool):
        errors.add("allowPublishWithoutRequest debe ser booleano")

    errors.raise_if_any()

    return Area(
        id=area_id,
        display_name=display_name,
        access_mode=access_mode,
        required_groups=tuple(groups),
        channels=tuple(channels),
        agent_trigger_url_secret=trigger_secret,
        agent_token_secret=token_secret,
        enabled=enabled,
        allow_publish_without_request=allow_without_request,
        updated_at=datetime.now(timezone.utc).isoformat(),
        updated_by=updated_by,
    )


def ensure_unique_across_areas(candidate: Area, existing: list[Area]) -> None:
    """Un alias y un canal sólo pueden pertenecer a un área.

    El enrutamiento de entrada busca el área por canal, por lo que la
    pertenencia debe ser única para ser determinista.
    """
    errors = AreaPayloadErrors()
    for other in existing:
        if other.id == candidate.id:
            continue
        for channel in candidate.channels:
            if other.channel_by_alias(channel.alias):
                errors.add(f"el alias {channel.alias!r} ya pertenece al área {other.id!r}")
            if other.channel_by_conversation(channel.conversation_id):
                errors.add(
                    f"el canal {channel.display_name or channel.alias!r} "
                    f"ya pertenece al área {other.id!r}"
                )
    errors.raise_if_any()


def area_to_entity(area: Area) -> dict[str, Any]:
    return {
        "PartitionKey": AREA_PARTITION_KEY,
        "RowKey": area.id,
        "display_name": area.display_name,
        "enabled": area.enabled,
        "access_mode": area.access_mode,
        "required_groups": json.dumps(list(area.required_groups)),
        "channels": json.dumps(
            [
                {
                    "alias": c.alias,
                    "conversation_id": c.conversation_id,
                    "display_name": c.display_name,
                }
                for c in area.channels
            ],
            ensure_ascii=False,
        ),
        "agent_trigger_url_secret": area.agent_trigger_url_secret,
        "agent_token_secret": area.agent_token_secret,
        "allow_publish_without_request": area.allow_publish_without_request,
        "updated_at": area.updated_at,
        "updated_by": area.updated_by,
    }


def area_from_entity(entity: dict[str, Any]) -> Area:
    """Convierte una fila de la tabla. Falla si la fila está corrupta."""
    try:
        groups = json.loads(entity.get("required_groups") or "[]")
        raw_channels = json.loads(entity.get("channels") or "[]")
    except json.JSONDecodeError as exc:
        raise AreaValidationError(
            f"Área {entity.get('RowKey')!r} con JSON inválido"
        ) from exc

    access_mode = str(entity.get("access_mode", "")).lower()
    if access_mode not in ACCESS_MODES or not groups:
        raise AreaValidationError(
            f"Área {entity.get('RowKey')!r} sin modo o grupos válidos"
        )

    return Area(
        id=str(entity["RowKey"]),
        display_name=str(entity.get("display_name", "")),
        access_mode=access_mode,
        required_groups=tuple(str(g).lower() for g in groups),
        channels=tuple(
            AreaChannel(
                alias=str(c.get("alias", "")),
                conversation_id=str(c.get("conversation_id", "")),
                display_name=str(c.get("display_name", "")),
            )
            for c in raw_channels
            if isinstance(c, dict)
        ),
        agent_trigger_url_secret=str(entity.get("agent_trigger_url_secret", "")),
        agent_token_secret=str(entity.get("agent_token_secret", "")),
        enabled=bool(entity.get("enabled", False)),
        allow_publish_without_request=bool(
            entity.get("allow_publish_without_request", False)
        ),
        updated_at=str(entity.get("updated_at", "")),
        updated_by=str(entity.get("updated_by", "")),
    )
