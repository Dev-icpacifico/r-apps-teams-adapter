"""Revisión de solicitudes desde el chat personal con el bot.

Los administradores (miembros del grupo de Entra TEAMS_ADMIN_GROUP_ID) abren
una vez el chat personal con el bot; desde entonces reciben allí cada
solicitud nueva como tarjeta con botones Aprobar / Rechazar. «Aprobar» aplica
la configuración pedida (habilita el equipo, agrega canales o agentes); la
asignación a grupos de Entra sigue siendo manual. Al resolverse, todas las
tarjetas enviadas para esa solicitud se reemplazan por una versión sin
botones con el resultado.
"""

from dataclasses import dataclass
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from access_requests import AREA_CHANGE_TYPES, REQUEST_TYPES, AccessRequest

ADMIN_CHAT_PARTITION_KEY = "TeamsAdminChat"
REVIEW_ACTION = "revisar_solicitud"
CARD_REVIEW_STATUSES = {"aprobada": "Aprobar", "rechazada": "Rechazar"}


@dataclass(frozen=True)
class AdminChat:
    aad_object_id: str
    conversation_id: str
    service_url: str
    name: str
    registered_at: str


class AdminChatStore:
    def __init__(self, table_client):
        self._table = table_client

    async def save(self, chat: AdminChat) -> None:
        await self._table.upsert_entity(entity={
            "PartitionKey": ADMIN_CHAT_PARTITION_KEY,
            "RowKey": chat.aad_object_id,
            "conversation_id": chat.conversation_id,
            "service_url": chat.service_url,
            "name": chat.name,
            "registered_at": chat.registered_at,
        })

    async def list(self) -> list[AdminChat]:
        result = []
        async for e in self._table.query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": ADMIN_CHAT_PARTITION_KEY}
        ):
            result.append(AdminChat(
                aad_object_id=str(e["RowKey"]),
                conversation_id=str(e.get("conversation_id", "")),
                service_url=str(e.get("service_url", "")),
                name=str(e.get("name", "")),
                registered_at=str(e.get("registered_at", "")),
            ))
        return result

    async def delete(self, aad_object_id: str) -> None:
        try:
            await self._table.delete_entity(partition_key=ADMIN_CHAT_PARTITION_KEY, row_key=aad_object_id)
        except ResourceNotFoundError:
            pass


def _fact(title: str, value: str) -> dict[str, str]:
    return {"title": title, "value": value or "—"}


def planned_changes(req: AccessRequest, area=None) -> list[str]:
    """Lo que «Aprobar» aplicará automáticamente. Con el área vigente se
    distingue lo que ya estaba configurado."""
    channels = ", ".join(c.get("name", "") for c in req.channels)
    agents = ", ".join(req.agents)
    if req.tipo == "nueva_area":
        changes = [f"Habilitar el equipo «{req.team_name or req.nueva_area}» en {channels or '—'} "
                   f"con {agents or '—'}. Regla común: el grupo de Entra que indiques abajo."]
        if req.publish:
            changes.append(f"Permitir publicar desde ChatGPT en {channels}. "
                           "Publicadores: el grupo de Entra que indiques abajo.")
        return changes
    if req.tipo not in AREA_CHANGE_TYPES:
        return []
    present = {c.conversation_id: c.alias for c in area.channels} if area is not None else {}
    area_agents = (set(area.agent_refs) | {a.alias for a in area.agents}) if area is not None else set()
    published = set(area.direct_publish.channels) if area is not None and area.direct_publish else set()
    changes = []
    if req.tipo != "agregar_agente":
        new = [c.get("name", "") for c in req.channels if c["conversationId"] not in present]
        old = [c.get("name", "") for c in req.channels if c["conversationId"] in present]
        if new:
            changes.append("Agregar los canales " + ", ".join(new) + ".")
        if old:
            changes.append("Ya estaban habilitados los canales " + ", ".join(old) + ".")
    new_agents = [a for a in req.agents if a not in area_agents]
    old_agents = [a for a in req.agents if a in area_agents]
    if new_agents:
        changes.append("Agregar los agentes " + ", ".join(new_agents) + ".")
    if old_agents:
        changes.append("Ya estaban los agentes " + ", ".join(old_agents) + ".")
    if req.publish:
        pending = [c.get("name", "") for c in req.channels if present.get(c["conversationId"]) not in published]
        if pending:
            changes.append("Permitir publicar desde ChatGPT en " + ", ".join(pending) + ".")
        else:
            changes.append("Ya se podía publicar desde ChatGPT en esos canales.")
    return changes


def needs_publisher_group(req: AccessRequest, area=None) -> bool:
    """La publicación pedida exige indicar el grupo de publicadores cuando
    el área todavía no tiene esa regla."""
    return req.publish and (req.tipo == "nueva_area" or area is None or area.direct_publish is None)


def build_review_card(req: AccessRequest, group_names: list[str], area=None) -> dict[str, Any]:
    """Tarjeta de revisión. Si la solicitud ya no está pendiente se muestra
    bloqueada: sin botones ni comentario, con el resultado."""
    pending = req.status == "pendiente"
    team = req.team_name or req.nueva_area
    if req.tipo == "nueva_area":
        team += " (sin agentes aún)"
    facts = [
        _fact("Estado", req.status),
        _fact("Equipo", team),
        _fact("Canales", ", ".join(c.get("name", "") for c in req.channels)),
        _fact("Agentes", ", ".join(req.agents)),
        _fact("Publicar desde ChatGPT", "Sí" if req.publish else "No"),
        _fact("Solicitante", req.requester_name),
        _fact("Motivo", req.justification),
    ]
    if not pending:
        facts += [_fact("Revisada por", req.reviewed_by), _fact("Fecha", req.reviewed_at[:16].replace("T", " ") + " UTC")]
        if req.review_comment:
            facts.append(_fact("Comentario", req.review_comment))
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": f"Solicitud {req.id}", "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": REQUEST_TYPES.get(req.tipo, req.tipo), "isSubtle": True, "spacing": "None", "wrap": True},
        {"type": "FactSet", "facts": facts},
    ]
    if req.people:
        body.append({"type": "TextBlock", "text": f"Personas ({len(req.people)})", "weight": "Bolder", "wrap": True})
        # Selector de personas precargado: Teams muestra los nombres a partir
        # del Object ID (el bot no tiene permiso para leer usuarios). Su valor
        # no se usa al revisar.
        body.append({
            "type": "Input.ChoiceSet",
            "id": "personas_solicitadas",
            "isMultiSelect": True,
            "value": ",".join(req.people),
            "choices": [],
            "choices.data": {"type": "Data.Query", "dataset": "graph.microsoft.com/users"},
        })
    if pending:
        changes = planned_changes(req, area)
        if changes:
            body.append({"type": "TextBlock", "text": "Al aprobar se aplica:", "weight": "Bolder", "wrap": True})
            body += [{"type": "TextBlock", "text": f"- {c}", "wrap": True, "spacing": "None"} for c in changes]
    elif req.applied:
        body.append({"type": "TextBlock", "text": "Aplicado:", "weight": "Bolder", "wrap": True})
        body += [{"type": "TextBlock", "text": f"- {a}", "wrap": True, "spacing": "None"} for a in req.applied]
    if req.people and group_names and req.status in ("pendiente", "aprobada"):
        body.append({"type": "TextBlock", "wrap": True,
                     "text": "Manual: asignar a las personas en Entra los grupos " + ", ".join(group_names) + "."})
    if not pending:
        body.append({"type": "TextBlock", "wrap": True, "weight": "Bolder",
                     "color": "Attention" if req.status == "rechazada" else "Good",
                     "text": f"Solicitud {req.status}. Ya no admite cambios desde esta tarjeta."})
        return {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.5",
            "body": body,
        }
    body.append({"type": "TextBlock", "wrap": True, "isSubtle": True,
                 "text": "La asignación de grupos en Entra nunca se hace automáticamente."})
    if req.tipo == "nueva_area":
        body.append({"type": "Input.Text", "id": "grupo_area",
                     "label": "Grupo de Entra del área (obligatorio para aprobar)",
                     "placeholder": "r-grpAg-Teams… o su Object ID", "maxLength": 256})
    if needs_publisher_group(req, area):
        body.append({"type": "Input.Text", "id": "grupo_publicadores",
                     "label": "Grupo de Entra de quienes pueden publicar desde ChatGPT (obligatorio para aprobar)",
                     "placeholder": "r-grpAg-Teams…-MCP o su Object ID", "maxLength": 256})
    body.append({"type": "Input.Text", "id": "comentario", "label": "Comentario para el solicitante",
                 "isMultiline": True, "maxLength": 1000})
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": body,
        "actions": [
            {"type": "Action.Submit", "title": title,
             "data": {"accion": REVIEW_ACTION, "id": req.id, "estado": status}}
            for status, title in CARD_REVIEW_STATUSES.items()
        ],
    }
