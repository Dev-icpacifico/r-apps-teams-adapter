"""Reglas de negocio del modo multiagente por áreas.

No depende del SDK de Teams ni de FastAPI, para poder probarse con
dobles de prueba.
"""

import html
import logging
import re
import uuid
from dataclasses import dataclass

from agents import AgentConfigurationError, AgentInvoker, SecretResolver, conversation_key
from areas import (
    RESERVED_AGENT_ALIASES,
    Area,
    AreaAgent,
    AreaChannel,
    AreaValidationError,
    ensure_unique_across_areas,
    parse_area_payload,
)
from graph_groups import GroupLookupError
from policy import evaluate_agent_access
from stores import AreaStore, Correlation, CorrelationStore, KnownChannel, KnownChannelStore, utc_now

logger = logging.getLogger("teams-adapter.areas")

MSG_ACCEPTED = "Solicitud recibida. Estoy procesándola."
MSG_NOT_CHANNEL = (
    "Este agente atiende solicitudes desde los canales de cada área. "
    "Escríbeme en el canal correspondiente."
)
MSG_CHANNEL_NOT_ENABLED = "Este canal no está habilitado para el agente."
MSG_FORBIDDEN = "No tienes los permisos requeridos para usar el agente en esta área."
MSG_ERROR = "No pude iniciar el procesamiento de la solicitud."
MSG_NO_AGENTS = "Esta área no tiene agentes configurados."


def agent_forbidden_message(agent: AreaAgent, allowed: list[AreaAgent], bot_name: str) -> str:
    text = f"No tienes permisos para usar **{agent.display_name}**."
    if allowed:
        mention = f"@{bot_name}" if bot_name else "@bot"
        names = ", ".join(f"**{a.alias}**" for a in allowed)
        text += f" Agentes disponibles para ti: {names}. Escribe: {mention} {allowed[0].alias} y tu solicitud."
    return text

_FIRST_WORD = re.compile(r"^\s*([^\s:,]+)\s*[:,]?\s*(.*)$", re.DOTALL)


def accepted_message(agent: AreaAgent) -> str:
    return f"Solicitud recibida. {agent.display_name} está procesándola."


def normalize_text(text: str) -> str:
    """Limpia el texto que llega de Teams (entidades HTML y espacios duros)."""
    text = html.unescape(text or "").replace("\xa0", " ")
    text = re.sub(r"</?(p|div|span|br)[^>]*>", " ", text, flags=re.IGNORECASE)
    return text.strip()


def format_agent_list(
    area: Area,
    bot_name: str = "",
    intro: str = "",
    agents: list[AreaAgent] | None = None,
) -> str:
    """Lista los agentes (por omisión todos los del área)."""
    mention = f"@{bot_name}" if bot_name else "@bot"
    agents = list(area.agents) if agents is None else agents
    if not agents:
        return MSG_NO_AGENTS
    lines = [intro or f"**Agentes disponibles en {area.display_name}:**", ""]
    for agent in agents:
        line = f"- **{agent.alias}**: {agent.display_name}"
        if agent.description:
            line += f". {agent.description}"
        lines.append(line)
    example = agents[0].alias
    lines += ["", f"Escribe: {mention} {example} y tu solicitud."]
    fallback = _fallback_agent(area, agents)
    if fallback is not None and len(agents) > 1:
        lines.append(f"Si no indicas un agente, responde **{fallback.alias}**.")
    return "\n".join(lines)


def _fallback_agent(area: Area, agents: list[AreaAgent]) -> AreaAgent | None:
    """Agente que atiende un mensaje sin alias: el predeterminado si está
    permitido; si no, el único permitido."""
    if area.default_agent:
        default = next((a for a in agents if a.alias == area.default_agent), None)
        if default is not None:
            return default
    if len(agents) == 1:
        return agents[0]
    return None


@dataclass(frozen=True)
class AgentRoute:
    kind: str  # list | agent | choose | empty
    agent: AreaAgent | None = None
    text: str = ""


def route_message(
    area: Area, text: str, allowed: list[AreaAgent] | None = None
) -> AgentRoute:
    """Decide a qué agente va el mensaje (texto ya sin la mención al bot).

    - «agentes», «ayuda» o mensaje vacío: lista los agentes.
    - «<alias> solicitud» (también «alias: …»): ese agente, aunque el
      usuario no tenga permiso (el llamador lo verifica y avisa).
    - sin alias: el predeterminado o el único entre los permitidos
      (``allowed``; por omisión todos); si no hay, se pide elegir.
    """
    allowed = list(area.agents) if allowed is None else allowed
    text = normalize_text(text)
    match = _FIRST_WORD.match(text)
    first = match.group(1).lower() if match else ""
    rest = match.group(2).strip() if match else ""

    if not text or first in RESERVED_AGENT_ALIASES:
        return AgentRoute("list")

    agent = area.agent_by_alias(first)
    if agent is not None:
        if not rest:
            return AgentRoute("empty", agent)
        return AgentRoute("agent", agent, rest)

    fallback = _fallback_agent(area, allowed)
    if fallback is not None:
        return AgentRoute("agent", fallback, text)
    return AgentRoute("choose")


@dataclass(frozen=True)
class IncomingMessage:
    text: str
    conversation_id: str
    channel_conversation_id: str | None
    channel_name: str
    team_id: str
    team_name: str
    team_aad_group_id: str
    service_url: str
    tenant_id: str
    user_id: str
    aad_object_id: str
    bot_id: str
    channel_id: str
    activity_id: str
    bot_name: str = ""

    def conversation_record(self) -> dict[str, str]:
        return {
            "service_url": self.service_url,
            "conversation_id": self.conversation_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "aad_object_id": self.aad_object_id,
            "bot_id": self.bot_id,
            "channel_id": self.channel_id,
            "activity_id": self.activity_id,
        }


@dataclass(frozen=True)
class InboundOutcome:
    # accepted | not_channel | channel_not_enabled | forbidden | error
    # | agents_list | choose_agent | missing_text | agent_forbidden
    status: str
    user_message: str
    correlation_id: str = ""
    area_id: str = ""
    agent_alias: str = ""


class RequestDenied(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


class AreaService:
    def __init__(
        self,
        areas: AreaStore,
        channels: KnownChannelStore,
        correlations: CorrelationStore,
        groups,
        agents: AgentInvoker,
        secrets: SecretResolver | None,
    ):
        self.areas = areas
        self.channels = channels
        self.correlations = correlations
        self.groups = groups
        self.agents = agents
        self.secrets = secrets

    # ------------------------------------------------------------
    # Entrada desde Teams
    # ------------------------------------------------------------

    async def find_area_for_channel(self, conversation_id: str) -> Area | None:
        for area in await self.areas.list_areas():
            if area.channel_by_conversation(conversation_id):
                return area
        return None

    async def handle_incoming(self, msg: IncomingMessage) -> InboundOutcome:
        if not msg.channel_conversation_id:
            return InboundOutcome("not_channel", MSG_NOT_CHANNEL)

        await self._record_channel(msg)

        area = await self.find_area_for_channel(msg.channel_conversation_id)
        if area is None or not area.enabled:
            logger.info("Canal sin área habilitada | canal=%s", msg.channel_name)
            return InboundOutcome("channel_not_enabled", MSG_CHANNEL_NOT_ENABLED)

        try:
            member = await self.groups.member_groups(msg.aad_object_id, area.all_groups())
        except GroupLookupError as exc:
            logger.error("No se pudo verificar grupos | area=%s | error=%s", area.id, exc)
            return InboundOutcome("forbidden", MSG_FORBIDDEN, area_id=area.id)

        decisions = {a.alias: evaluate_agent_access(area, a, member) for a in area.agents}
        allowed = [a for a in area.agents if decisions[a.alias].allowed]
        if not allowed:
            logger.warning(
                "Acceso denegado | area=%s | motivo=%s",
                area.id,
                "; ".join(d.reason for d in decisions.values()) or "sin agentes",
            )
            return InboundOutcome("forbidden", MSG_FORBIDDEN, area_id=area.id)

        route = route_message(area, msg.text, allowed)
        if route.kind == "list":
            return InboundOutcome(
                "agents_list",
                format_agent_list(
                    area,
                    msg.bot_name,
                    intro=f"**Agentes disponibles para ti en {area.display_name}:**",
                    agents=allowed,
                ),
                area_id=area.id,
            )
        if route.kind == "choose":
            return InboundOutcome(
                "choose_agent",
                format_agent_list(
                    area,
                    msg.bot_name,
                    intro="Indica qué agente debe atender tu solicitud. "
                    f"**Agentes disponibles para ti en {area.display_name}:**",
                    agents=allowed,
                ),
                area_id=area.id,
            )
        if route.agent is not None and not decisions[route.agent.alias].allowed:
            logger.warning(
                "Acceso denegado al agente | area=%s | agente=%s | motivo=%s",
                area.id, route.agent.alias, decisions[route.agent.alias].reason,
            )
            return InboundOutcome(
                "agent_forbidden",
                agent_forbidden_message(route.agent, allowed, msg.bot_name),
                area_id=area.id,
                agent_alias=route.agent.alias,
            )
        if route.kind == "empty":
            mention = f"@{msg.bot_name}" if msg.bot_name else "@bot"
            return InboundOutcome(
                "missing_text",
                f"Escribe tu solicitud después del alias: {mention} {route.agent.alias} …",
                area_id=area.id,
                agent_alias=route.agent.alias,
            )

        agent = route.agent
        correlation_id = str(uuid.uuid4())
        try:
            await self.correlations.save(
                correlation_id,
                msg.conversation_record(),
                area_id=area.id,
                channel_conversation_id=msg.channel_conversation_id,
                agent_alias=agent.alias,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error guardando correlación | area=%s", area.id)
            return InboundOutcome("error", MSG_ERROR, area_id=area.id)

        try:
            status_code = await self.agents.trigger(
                area,
                agent,
                route.text,
                correlation_id,
                conversation_key=conversation_key(area.id, agent.alias, msg.conversation_id),
                idempotency_key=f"teams-{msg.activity_id}" if msg.activity_id else correlation_id,
            )
        except (AgentConfigurationError, Exception):  # noqa: BLE001
            logger.exception(
                "Error al iniciar el agente | area=%s | agente=%s | correlation_id=%s",
                area.id, agent.alias, correlation_id,
            )
            return InboundOutcome("error", MSG_ERROR, correlation_id, area.id, agent.alias)

        if status_code in (200, 201, 202):
            logger.info(
                "Agente iniciado | area=%s | agente=%s | correlation_id=%s",
                area.id, agent.alias, correlation_id,
            )
            return InboundOutcome(
                "accepted", accepted_message(agent), correlation_id, area.id, agent.alias
            )

        logger.warning(
            "Agente respondió HTTP %s | area=%s | agente=%s | correlation_id=%s",
            status_code, area.id, agent.alias, correlation_id,
        )
        return InboundOutcome("error", MSG_ERROR, correlation_id, area.id, agent.alias)

    async def _record_channel(self, msg: IncomingMessage) -> None:
        try:
            await self.channels.record(
                KnownChannel(
                    conversation_id=msg.channel_conversation_id or "",
                    channel_name=msg.channel_name,
                    team_id=msg.team_id,
                    team_name=msg.team_name,
                    team_aad_group_id=msg.team_aad_group_id,
                    service_url=msg.service_url,
                    last_seen_at=utc_now().isoformat(),
                )
            )
        except Exception:  # noqa: BLE001 - registrar canales no debe bloquear
            logger.exception("No se pudo registrar el canal conocido")

    # ------------------------------------------------------------
    # Respuesta (callback) y publicación desde el MCP
    # ------------------------------------------------------------

    async def authorize_callback(self, correlation_id: str) -> Correlation:
        correlation = await self.correlations.get(correlation_id)
        if correlation is None:
            raise RequestDenied(404, "correlation_id no encontrado")
        if correlation.is_expired():
            raise RequestDenied(410, "correlation_id expirado")
        if correlation.responded_at:
            raise RequestDenied(409, "La solicitud ya fue respondida")
        return correlation

    async def resolve_publish(
        self, alias: str, correlation_id: str | None
    ) -> tuple[Area, AreaChannel]:
        alias = (alias or "").strip().lower()
        target: tuple[Area, AreaChannel] | None = None
        for area in await self.areas.list_areas():
            channel = area.channel_by_alias(alias)
            if channel:
                target = (area, channel)
                break

        if target is None:
            raise RequestDenied(404, "Destino Teams no encontrado")

        area, channel = target
        if not area.enabled:
            raise RequestDenied(403, "El área del destino está deshabilitada")

        if correlation_id:
            correlation = await self.correlations.get(correlation_id)
            if correlation is None or correlation.is_expired():
                raise RequestDenied(403, "Solicitud de origen no válida o expirada")
            if correlation.area_id != area.id:
                raise RequestDenied(
                    403, "El destino no pertenece al área de la solicitud de origen"
                )
            return area, channel

        if not area.allow_publish_without_request:
            raise RequestDenied(
                403, "Este destino requiere una solicitud de origen (correlation_id)"
            )
        return area, channel

    # ------------------------------------------------------------
    # Administración
    # ------------------------------------------------------------

    async def upsert_area(self, area_id: str, payload, actor: str) -> Area:
        area = parse_area_payload(area_id, payload, updated_by=actor)
        existing = await self.areas.list_areas(use_cache=False)
        ensure_unique_across_areas(area, existing)

        unknown = [
            c.alias for c in area.channels
            if not await self.channels.exists(c.conversation_id)
        ]
        if unknown:
            raise AreaValidationError(
                "Canales sin actividad del bot (instalar el bot y escribirle una vez): "
                + ", ".join(unknown)
            )

        if self.secrets is None:
            raise AreaValidationError("KEY_VAULT_URI no configurada en el adaptador")
        without_token = [
            a.alias for a in area.agents if not self.agents.token_secret_for(a)
        ]
        if without_token:
            raise AreaValidationError(
                "Agentes sin token (definir tokenSecret o AGENT_DEFAULT_TOKEN_SECRET): "
                + ", ".join(without_token)
            )
        names = sorted({self.agents.token_secret_for(a) for a in area.agents})
        missing = [name for name in names if not await self.secrets.exists(name)]
        if missing:
            raise AreaValidationError(
                "Secretos no encontrados en Key Vault: " + ", ".join(missing)
            )

        await self.areas.save_area(area)
        logger.info("Área guardada | area=%s | por=%s", area.id, actor)
        return area

    async def set_enabled(self, area_id: str, enabled: bool, actor: str) -> Area:
        area = await self.areas.get_area(area_id)
        if area is None:
            raise RequestDenied(404, "Área no encontrada")
        updated = Area(
            **{
                **area.__dict__,
                "enabled": enabled,
                "updated_at": utc_now().isoformat(),
                "updated_by": actor,
            }
        )
        await self.areas.save_area(updated)
        logger.info("Área %s | area=%s | por=%s", "habilitada" if enabled else "deshabilitada", area_id, actor)
        return updated
