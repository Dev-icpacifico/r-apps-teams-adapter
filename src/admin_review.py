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

from access_requests import REQUEST_TYPES, AccessRequest

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


def planned_changes(req: AccessRequest) -> list[str]:
    """Lo que «Aprobar» aplicará automáticamente."""
    channels = ", ".join(c.get("name", "") for c in req.channels)
    agents = ", ".join(req.agents)
    if req.tipo == "nueva_area":
        return [f"Habilitar el equipo «{req.team_name or req.nueva_area}» en {channels or '—'} "
                f"con {agents or '—'}. Regla común: el grupo de Entra que indiques abajo."]
    changes = []
    if req.tipo == "agregar_canal" and channels:
        changes.append(f"Agregar los canales {channels}.")
    if req.tipo in ("agregar_canal", "agregar_agente") and agents:
        changes.append(f"Agregar los agentes {agents}.")
    return changes


def build_review_card(req: AccessRequest, group_names: list[str]) -> dict[str, Any]:
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
        changes = planned_changes(req)
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
