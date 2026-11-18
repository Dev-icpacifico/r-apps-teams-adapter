"""Solicitudes de acceso y configuración desde Teams.

``@Bot solicitar`` muestra una tarjeta adaptable para pedir un área nueva,
canales, agentes o acceso para personas. Enviarla **sólo registra una
solicitud pendiente**: nada se configura ni se asigna automáticamente. Un
administrador la revisa (API /api/admin/requests o AgentesTeams.ps1), aplica
los cambios que correspondan —incluida la asignación a grupos de Entra, que
es siempre manual— y marca el resultado, que se avisa en el hilo original.
"""

import json
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

from areas import GUID_PATTERN

REQUEST_PARTITION_KEY = "TeamsRequest"
SUBMIT_ACTION = "solicitud_agentes"
CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

REQUEST_TYPES = {
    "nueva_area": "Crear un área nueva",
    "agregar_canal": "Agregar canales a un área",
    "agregar_agente": "Agregar agentes a un área",
    "dar_acceso": "Dar acceso a personas",
}
STATUSES = ("pendiente", "aprobada", "rechazada", "aplicada")
NEW_AREA = "__nueva__"
MAX_PEOPLE = 50
MAX_TEXT = 1000

_REQUEST_ID = re.compile(r"^SOL-\d{8}-[A-Z0-9]{4}$")


class RequestValidationError(ValueError):
    """Datos de solicitud inválidos; el mensaje es apto para el usuario."""


def new_request_id(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return f"SOL-{now:%Y%m%d}-" + "".join(secrets.choice(alphabet) for _ in range(4))


def is_request_id(value: str) -> bool:
    return bool(_REQUEST_ID.match(value or ""))


# ------------------------------------------------------------
# Tarjeta
# ------------------------------------------------------------

def build_request_card(
    team_name: str,
    channels: list[tuple[str, str]],
    current_channel: str,
    areas: list[tuple[str, str]],
    agents: list[tuple[str, str]],
) -> dict[str, Any]:
    """Tarjeta adaptable 1.5.

    channels: (conversation_id, nombre) de canales estándar del equipo.
    areas: (id, nombre). agents: (alias, nombre del catálogo).
    """
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [
            {"type": "TextBlock", "text": "Solicitud para Agentes ICP", "weight": "Bolder", "size": "Medium", "wrap": True},
            {
                "type": "TextBlock",
                "text": "Se enviará a revisión. No se aplica ningún cambio ni se asignan grupos hasta que un administrador la apruebe.",
                "wrap": True, "isSubtle": True, "spacing": "Small",
            },
            {
                "type": "Input.ChoiceSet", "id": "tipo", "label": "¿Qué necesitas?", "isRequired": True,
                "errorMessage": "Elige un tipo de solicitud", "value": "dar_acceso",
                "choices": [{"title": t, "value": v} for v, t in REQUEST_TYPES.items()],
            },
            {
                "type": "Input.ChoiceSet", "id": "area", "label": "Área", "value": areas[0][0] if areas else NEW_AREA,
                "choices": [{"title": f"{n} ({i})", "value": i} for i, n in areas]
                + [{"title": "Área nueva", "value": NEW_AREA}],
            },
            {"type": "Input.Text", "id": "nueva_area", "label": "Nombre del área nueva (solo si corresponde)", "maxLength": 100},
            {
                "type": "Input.ChoiceSet", "id": "canales", "label": f"Canales de «{team_name}»",
                "isMultiSelect": True, "value": current_channel if any(c == current_channel for c, _ in channels) else "",
                "choices": [{"title": n, "value": c} for c, n in channels],
            },
            {
                "type": "Input.ChoiceSet", "id": "agentes", "label": "Agentes", "isMultiSelect": True,
                "choices": [{"title": n, "value": a} for a, n in agents],
            },
            {
                "type": "Input.ChoiceSet", "id": "personas", "label": "Personas que necesitan acceso",
                "isMultiSelect": True,
                "choices": [],
                "choices.data": {"type": "Data.Query", "dataset": "graph.microsoft.com/users"},
            },
            {
                "type": "Input.Text", "id": "justificacion", "label": "Motivo", "isMultiline": True,
                "isRequired": True, "maxLength": MAX_TEXT, "errorMessage": "Explica brevemente para qué se necesita",
            },
        ],
        "actions": [
            {"type": "Action.Submit", "title": "Enviar solicitud", "data": {"accion": SUBMIT_ACTION}},
        ],
    }


def card_attachment(card: dict[str, Any]) -> dict[str, Any]:
    return {"contentType": CARD_CONTENT_TYPE, "content": card}


# ------------------------------------------------------------
# Solicitud
# ------------------------------------------------------------

@dataclass
class AccessRequest:
    id: str
    tipo: str
    area_id: str
    nueva_area: str
    team_id: str
    team_name: str
    channels: list[dict[str, str]]  # {conversationId, name}
    agents: list[str]
    people: list[str]  # Object ID de Entra
    justification: str
    requester_oid: str
    requester_name: str
    conversation: dict[str, str] = field(default_factory=dict)
    status: str = "pendiente"
    review_comment: str = ""
    reviewed_by: str = ""
    created_at: str = ""
    reviewed_at: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tipo": self.tipo,
            "tipoTexto": REQUEST_TYPES.get(self.tipo, self.tipo),
            "areaId": self.area_id,
            "nuevaArea": self.nueva_area,
            "teamId": self.team_id,
            "teamName": self.team_name,
            "channels": self.channels,
            "agents": self.agents,
            "people": self.people,
            "justificacion": self.justification,
            "requesterOid": self.requester_oid,
            "requesterName": self.requester_name,
            "status": self.status,
            "reviewComment": self.review_comment,
            "reviewedBy": self.reviewed_by,
            "createdAt": self.created_at,
            "reviewedAt": self.reviewed_at,
        }


def _split(value: Any) -> list[str]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, str):
        items = value.split(",")
    else:
        return []
    return [str(i).strip() for i in items if str(i).strip()]


def parse_submission(
    value: dict[str, Any],
    team_channels: dict[str, str],
    area_ids: set[str],
    agent_aliases: set[str],
) -> dict[str, Any]:
    """Valida los datos enviados por la tarjeta. team_channels: id -> nombre
    de los canales estándar del equipo; nada fuera de esas listas se acepta."""
    tipo = str(value.get("tipo") or "").strip()
    if tipo not in REQUEST_TYPES:
        raise RequestValidationError("Elige un tipo de solicitud válido.")

    area = str(value.get("area") or "").strip()
    nueva = str(value.get("nueva_area") or "").strip()[:100]
    if tipo == "nueva_area" or area == NEW_AREA:
        if not nueva:
            raise RequestValidationError("Indica el nombre del área nueva.")
        area = ""
    elif area not in area_ids:
        raise RequestValidationError("Elige un área existente o «Área nueva».")

    channels = []
    for cid in _split(value.get("canales")):
        if cid not in team_channels:
            raise RequestValidationError("Hay canales que no pertenecen a este equipo.")
        channels.append({"conversationId": cid, "name": team_channels[cid]})

    agents = _split(value.get("agentes"))
    unknown = [a for a in agents if a not in agent_aliases]
    if unknown:
        raise RequestValidationError("Hay agentes que no existen en el catálogo.")

    people = [p.lower() for p in _split(value.get("personas"))]
    if any(not GUID_PATTERN.match(p) for p in people):
        raise RequestValidationError("Elige a las personas desde la lista del formulario.")
    if len(people) > MAX_PEOPLE:
        raise RequestValidationError(f"Máximo {MAX_PEOPLE} personas por solicitud.")

    justification = str(value.get("justificacion") or "").strip()
    if not justification:
        raise RequestValidationError("Explica brevemente el motivo de la solicitud.")
    if len(justification) > MAX_TEXT:
        raise RequestValidationError(f"El motivo admite como máximo {MAX_TEXT} caracteres.")

    if tipo in ("nueva_area", "agregar_canal") and not channels:
        raise RequestValidationError("Elige al menos un canal.")
    if tipo == "agregar_agente" and not agents:
        raise RequestValidationError("Elige al menos un agente.")
    if tipo == "dar_acceso" and not people:
        raise RequestValidationError("Elige al menos una persona.")

    return {
        "tipo": tipo, "area_id": area, "nueva_area": nueva, "channels": channels,
        "agents": sorted(set(agents)), "people": list(dict.fromkeys(people)),
        "justification": justification,
    }


def request_summary(req: AccessRequest) -> str:
    lines = [f"**Solicitud {req.id}** · {REQUEST_TYPES.get(req.tipo, req.tipo)}"]
    lines.append(f"- Área: {req.area_id or ('nueva: ' + req.nueva_area)}")
    if req.channels:
        lines.append("- Canales: " + ", ".join(c["name"] for c in req.channels) + f" ({req.team_name})")
    if req.agents:
        lines.append("- Agentes: " + ", ".join(req.agents))
    if req.people:
        lines.append(f"- Personas: {len(req.people)}")
    lines.append(f"- Solicitante: {req.requester_name or req.requester_oid}")
    return "\n".join(lines)


# ------------------------------------------------------------
# Almacenamiento
# ------------------------------------------------------------

def request_to_entity(req: AccessRequest) -> dict[str, Any]:
    return {
        "PartitionKey": REQUEST_PARTITION_KEY,
        "RowKey": req.id,
        "tipo": req.tipo,
        "area_id": req.area_id,
        "nueva_area": req.nueva_area,
        "team_id": req.team_id,
        "team_name": req.team_name,
        "channels": json.dumps(req.channels, ensure_ascii=False),
        "agents": json.dumps(req.agents),
        "people": json.dumps(req.people),
        "justification": req.justification,
        "requester_oid": req.requester_oid,
        "requester_name": req.requester_name,
        "conversation": json.dumps(req.conversation),
        "status": req.status,
        "review_comment": req.review_comment,
        "reviewed_by": req.reviewed_by,
        "created_at": req.created_at,
        "reviewed_at": req.reviewed_at,
    }


def request_from_entity(e: dict[str, Any]) -> AccessRequest:
    def j(name, default):
        try:
            v = json.loads(e.get(name) or "null")
        except json.JSONDecodeError:
            return default
        return v if isinstance(v, type(default)) else default

    return AccessRequest(
        id=str(e["RowKey"]),
        tipo=str(e.get("tipo", "")),
        area_id=str(e.get("area_id", "")),
        nueva_area=str(e.get("nueva_area", "")),
        team_id=str(e.get("team_id", "")),
        team_name=str(e.get("team_name", "")),
        channels=j("channels", []),
        agents=j("agents", []),
        people=j("people", []),
        justification=str(e.get("justification", "")),
        requester_oid=str(e.get("requester_oid", "")),
        requester_name=str(e.get("requester_name", "")),
        conversation=j("conversation", {}),
        status=str(e.get("status", "pendiente")),
        review_comment=str(e.get("review_comment", "")),
        reviewed_by=str(e.get("reviewed_by", "")),
        created_at=str(e.get("created_at", "")),
        reviewed_at=str(e.get("reviewed_at", "")),
    )


class RequestStore:
    """Solicitudes en la tabla de áreas, con PartitionKey propia."""

    def __init__(self, table_client):
        self._table = table_client

    async def save(self, req: AccessRequest) -> None:
        await self._table.upsert_entity(entity=request_to_entity(req))

    async def get(self, request_id: str) -> AccessRequest | None:
        try:
            e = await self._table.get_entity(partition_key=REQUEST_PARTITION_KEY, row_key=request_id)
        except ResourceNotFoundError:
            return None
        return request_from_entity(e)

    async def list(self, status: str = "") -> list[AccessRequest]:
        result = []
        async for e in self._table.query_entities(
            query_filter="PartitionKey eq @pk", parameters={"pk": REQUEST_PARTITION_KEY}
        ):
            req = request_from_entity(e)
            if not status or req.status == status:
                result.append(req)
        return sorted(result, key=lambda r: r.created_at, reverse=True)

