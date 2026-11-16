"""Catálogo global de agentes.

Cada agente se define una sola vez: alias, canal API (``agtch_…``), token y
la regla de grupos de Entra que exige para verlo y usarlo. Esa regla viaja
con el agente y se aplica en cualquier área que lo use; el área sólo puede
sumar su propia restricción. Las áreas referencian agentes por alias.

Una regla con varios grupos en modo «any» permite modelar agentes con áreas
segregadas (p. ej. UnyAgent: cualquiera de los grupos lectores de Unysoft).
"""

import json
import logging
import time
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from areas import (
    ACCESS_MODES,
    AGENT_ALIAS_PATTERN,
    GUID_PATTERN,
    MAX_DESCRIPTION,
    MAX_DISPLAY_NAME,
    MAX_GROUPS,
    RESERVED_AGENT_ALIASES,
    SECRET_NAME_PATTERN,
    TRIGGER_ID_PATTERN,
    AreaAgent,
    AreaPayloadErrors,
    AreaValidationError,
)

AGENT_PARTITION_KEY = "TeamsAgent"

logger = logging.getLogger("teams-adapter.catalog")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_agent_payload(alias: str, payload: Any) -> AreaAgent:
    """Valida la definición de un agente del catálogo."""
    if not isinstance(payload, dict):
        raise AreaValidationError("El cuerpo debe ser un objeto JSON")
    errors = AreaPayloadErrors()

    alias = _text(alias).lower()
    if not AGENT_ALIAS_PATTERN.match(alias):
        errors.add(f"alias de agente inválido: {alias!r} (minúsculas, números, _ o -; 2-32)")
    elif alias in RESERVED_AGENT_ALIASES:
        errors.add(f"alias de agente reservado: {alias!r}")

    display_name = _text(payload.get("displayName"))
    description = _text(payload.get("description"))
    trigger_id = _text(payload.get("triggerId"))
    token_secret = _text(payload.get("tokenSecret"))
    if not display_name or len(display_name) > MAX_DISPLAY_NAME:
        errors.add("displayName es obligatorio (máx. 200 caracteres)")
    if len(description) > MAX_DESCRIPTION:
        errors.add(f"description supera {MAX_DESCRIPTION} caracteres")
    if not TRIGGER_ID_PATTERN.match(trigger_id):
        errors.add("triggerId inválido (se espera agtch_…)")
    if token_secret and not SECRET_NAME_PATTERN.match(token_secret):
        errors.add("tokenSecret debe ser el nombre de un secreto de Key Vault")

    raw_mode = payload.get("accessMode")
    access_mode = _text(raw_mode).lower() if raw_mode is not None else "any"
    if access_mode not in ACCESS_MODES:
        errors.add("accessMode debe ser 'any' o 'all'")
    raw_groups = payload.get("requiredGroups", [])
    groups: list[str] = []
    if not isinstance(raw_groups, list):
        errors.add("requiredGroups debe ser una lista de Object ID")
    elif len(raw_groups) > MAX_GROUPS:
        errors.add(f"requiredGroups admite como máximo {MAX_GROUPS} grupos")
    else:
        for value in raw_groups:
            group = _text(value).lower()
            if not GUID_PATTERN.match(group):
                errors.add(f"grupo inválido (se espera Object ID): {value!r}")
            elif group not in groups:
                groups.append(group)

    errors.raise_if_any()
    return AreaAgent(
        alias=alias,
        display_name=display_name,
        trigger_id=trigger_id,
        description=description,
        token_secret=token_secret,
        access_mode=access_mode,
        required_groups=tuple(groups),
    )


def agent_to_entity(agent: AreaAgent, updated_at: str = "", updated_by: str = "") -> dict[str, Any]:
    return {
        "PartitionKey": AGENT_PARTITION_KEY,
        "RowKey": agent.alias,
        "display_name": agent.display_name,
        "description": agent.description,
        "trigger_id": agent.trigger_id,
        "token_secret": agent.token_secret,
        "access_mode": agent.access_mode,
        "required_groups": json.dumps(list(agent.required_groups)),
        "updated_at": updated_at,
        "updated_by": updated_by,
    }


def agent_from_entity(entity: dict[str, Any]) -> AreaAgent:
    try:
        groups = json.loads(entity.get("required_groups") or "[]")
    except json.JSONDecodeError as exc:
        raise AreaValidationError(f"Agente {entity.get('RowKey')!r} con JSON inválido") from exc
    trigger_id = str(entity.get("trigger_id", ""))
    access_mode = str(entity.get("access_mode", "any")).lower()
    if not TRIGGER_ID_PATTERN.match(trigger_id) or access_mode not in ACCESS_MODES:
        raise AreaValidationError(f"Agente {entity.get('RowKey')!r} con datos inválidos")
    return AreaAgent(
        alias=str(entity["RowKey"]),
        display_name=str(entity.get("display_name", "")),
        trigger_id=trigger_id,
        description=str(entity.get("description", "")),
        token_secret=str(entity.get("token_secret", "")),
        access_mode=access_mode,
        required_groups=tuple(str(g).lower() for g in groups if isinstance(g, str)),
    )


class AgentCatalogStore:
    """Agentes en la tabla de áreas, con PartitionKey propia."""

    def __init__(self, table_client, cache_seconds: int = 60):
        self._table = table_client
        self._cache_seconds = cache_seconds
        self._cache: dict[str, AreaAgent] | None = None
        self._cache_at = 0.0

    def invalidate(self) -> None:
        self._cache = None

    async def all(self, use_cache: bool = True) -> dict[str, AreaAgent]:
        if (
            use_cache
            and self._cache is not None
            and time.monotonic() - self._cache_at < self._cache_seconds
        ):
            return self._cache
        agents: dict[str, AreaAgent] = {}
        async for entity in self._table.query_entities(
            query_filter="PartitionKey eq @pk",
            parameters={"pk": AGENT_PARTITION_KEY},
        ):
            try:
                agent = agent_from_entity(entity)
            except AreaValidationError:
                logger.error("Agente omitido por datos inválidos | agente=%s", entity.get("RowKey"))
                continue
            agents[agent.alias] = agent
        self._cache = agents
        self._cache_at = time.monotonic()
        return agents

    async def get(self, alias: str) -> AreaAgent | None:
        try:
            entity = await self._table.get_entity(partition_key=AGENT_PARTITION_KEY, row_key=alias)
        except ResourceNotFoundError:
            return None
        return agent_from_entity(entity)

    async def save(self, agent: AreaAgent, updated_at: str, updated_by: str) -> None:
        await self._table.upsert_entity(entity=agent_to_entity(agent, updated_at, updated_by))
        self.invalidate()

    async def delete(self, alias: str) -> None:
        try:
            await self._table.delete_entity(partition_key=AGENT_PARTITION_KEY, row_key=alias)
        except ResourceNotFoundError:
            pass
        self.invalidate()
