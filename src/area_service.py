"""Reglas de negocio del modo multiagente por áreas.

No depende del SDK de Teams ni de FastAPI, para poder probarse con
dobles de prueba.
"""

import logging
import uuid
from dataclasses import dataclass

from agents import AgentConfigurationError, AgentInvoker, SecretResolver
from areas import Area, AreaChannel, ensure_unique_across_areas, parse_area_payload, AreaValidationError
from graph_groups import GroupLookupError
from policy import evaluate_access
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
    status: str  # accepted | not_channel | channel_not_enabled | forbidden | error
    user_message: str
    correlation_id: str = ""
    area_id: str = ""


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
            member = await self.groups.member_groups(
                msg.aad_object_id, list(area.required_groups)
            )
        except GroupLookupError as exc:
            logger.error("No se pudo verificar grupos | area=%s | error=%s", area.id, exc)
            return InboundOutcome("forbidden", MSG_FORBIDDEN, area_id=area.id)

        decision = evaluate_access(area.access_mode, area.required_groups, member)
        if not decision.allowed:
            logger.warning(
                "Acceso denegado | area=%s | motivo=%s", area.id, decision.reason
            )
            return InboundOutcome("forbidden", MSG_FORBIDDEN, area_id=area.id)

        correlation_id = str(uuid.uuid4())
        try:
            await self.correlations.save(
                correlation_id,
                msg.conversation_record(),
                area_id=area.id,
                channel_conversation_id=msg.channel_conversation_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Error guardando correlación | area=%s", area.id)
            return InboundOutcome("error", MSG_ERROR, area_id=area.id)

        try:
            status_code = await self.agents.trigger(area, msg.text, correlation_id)
        except (AgentConfigurationError, Exception):  # noqa: BLE001
            logger.exception(
                "Error al iniciar el agente | area=%s | correlation_id=%s",
                area.id, correlation_id,
            )
            return InboundOutcome("error", MSG_ERROR, correlation_id, area.id)

        if status_code == 202:
            logger.info("Agente iniciado | area=%s | correlation_id=%s", area.id, correlation_id)
            return InboundOutcome("accepted", MSG_ACCEPTED, correlation_id, area.id)

        logger.warning(
            "Agente respondió HTTP %s | area=%s | correlation_id=%s",
            status_code, area.id, correlation_id,
        )
        return InboundOutcome("error", MSG_ERROR, correlation_id, area.id)

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
        missing = [
            name for name in (area.agent_trigger_url_secret, area.agent_token_secret)
            if not await self.secrets.exists(name)
        ]
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
