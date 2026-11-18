"""Revisión de solicitudes desde el chat personal con el bot.

Los administradores (miembros del grupo de Entra TEAMS_ADMIN_GROUP_ID) abren
una vez el chat personal con el bot; desde entonces reciben allí cada
solicitud nueva como tarjeta con botones Aprobar / Rechazar / Aplicada.
Los botones **sólo cambian el estado** y avisan al solicitante: no crean
áreas ni asignan grupos. La tarjeta muestra los pasos manuales pendientes.
"""

from dataclasses import dataclass
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from access_requests import REQUEST_TYPES, AccessRequest

ADMIN_CHAT_PARTITION_KEY = "TeamsAdminChat"
REVIEW_ACTION = "revisar_solicitud"
REVIEW_STATUSES = {"aprobada": "Aprobar", "rechazada": "Rechazar", "aplicada": "Marcar aplicada"}


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


def build_review_card(req: AccessRequest, group_names: list[str], next_steps: list[str]) -> dict[str, Any]:
    area = req.area_id or f"nueva: {req.nueva_area}"
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": f"Solicitud {req.id}", "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": REQUEST_TYPES.get(req.tipo, req.tipo), "isSubtle": True, "spacing": "None", "wrap": True},
        {"type": "FactSet", "facts": [
            _fact("Estado", req.status),
            _fact("Área", area),
            _fact("Equipo", req.team_name),
            _fact("Canales", ", ".join(c.get("name", "") for c in req.channels)),
            _fact("Agentes", ", ".join(req.agents)),
            _fact("Solicitante", req.requester_name),
            _fact("Motivo", req.justification),
        ]},
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
    if group_names:
        body.append({"type": "TextBlock", "wrap": True,
                     "text": "Grupos que necesitarán (asignación manual en Entra): " + ", ".join(group_names)})
    if next_steps:
        body.append({"type": "TextBlock", "text": "Pasos manuales:", "weight": "Bolder", "wrap": True})
        body += [{"type": "TextBlock", "text": f"- {s}", "wrap": True, "spacing": "None"} for s in next_steps]
    body.append({"type": "TextBlock", "wrap": True, "isSubtle": True,
                 "text": "Los botones solo registran tu decisión y avisan al solicitante; no aplican cambios."})
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
            for status, title in REVIEW_STATUSES.items()
        ],
    }


def next_steps_for(req: AccessRequest) -> list[str]:
    steps = []
    team = f" -Equipo '{req.team_name}'" if req.team_name else ""
    if not req.area_id:
        channels = ", ".join(f"'{c.get('name', '')}'" for c in req.channels) or "<canales>"
        agents = ", ".join(req.agents) or "<agentes>"
        steps.append(f"Crear el área «{req.nueva_area}»: New-AgentesArea -Area <id> -Nombre '{req.nueva_area}' "
                     f"-Canales {channels}{team} -Agentes {agents}")
    else:
        for c in req.channels:
            steps.append(f"Agregar canal: Add-AgentesCanal -Area {req.area_id} -Alias <alias> -Canal '{c.get('name', '')}'{team}")
        for a in req.agents:
            steps.append(f"Agregar agente: Add-AgentesAgenteArea -Area {req.area_id} -Alias {a}")
    if req.people:
        steps.append("Asignar a las personas los grupos indicados, en Entra.")
    return steps
