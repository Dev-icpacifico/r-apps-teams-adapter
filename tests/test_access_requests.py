"""Formulario de solicitudes en Teams: se registra, no se aplica."""

import asyncio
from dataclasses import replace

import pytest

from access_requests import (
    SUBMIT_ACTION,
    RequestValidationError,
    build_request_card,
    is_request_id,
    new_request_id,
    parse_submission,
    request_from_entity,
    request_to_entity,
)
from area_service import RequestDenied
from conftest import CH_RRHH, CH_RRHH_REPORTES, make_area
from stores import KnownChannel
from test_admin import client, token
from test_area_service import build_service, message

AUTH = {"Authorization": token()}
P1 = "aaaaaaaa-1111-2222-3333-444444444444"
P2 = "bbbbbbbb-1111-2222-3333-444444444444"
CH_PRIV = "19:privado@thread.tacv2"


def run(coro):
    return asyncio.run(coro)


class FakeRequests:
    def __init__(self):
        self.items = {}

    async def save(self, req):
        self.items[req.id] = req

    async def get(self, request_id):
        return self.items.get(request_id)

    async def list(self, status=""):
        return [r for r in self.items.values() if not status or r.status == status]


def known(cid, name, team="team-1", channel_type="standard"):
    return KnownChannel(cid, name, team, "RRHH", "", "https://smba", "2026-11-18", channel_type)


def service():
    svc = build_service(areas=[make_area()])
    svc.requests = FakeRequests()
    svc.channels.recorded = [
        known(CH_RRHH, "General"), known(CH_RRHH_REPORTES, "Reportes"),
        known(CH_PRIV, "Interno", channel_type="private"), known("19:otro", "Otro", team="team-2"),
    ]
    return svc


def submit(value, user="user-1"):
    return replace(message(user=user, text=""), value={"accion": SUBMIT_ACTION, **value}, user_name="Luis")


# ---------------- Tarjeta ----------------

def test_solicitar_returns_card_even_without_area():
    svc = service()
    outcome = run(svc.handle_incoming(message(channel="19:sin-area@thread.tacv2", text="solicitar")))
    assert outcome.status == "request_form" and outcome.attachment["contentType"].endswith("adaptive")


def test_card_lists_only_standard_channels_of_the_team_and_catalog():
    svc = service()
    card = run(svc.handle_incoming(message(text="solicitar"))).attachment["content"]
    inputs = {b.get("id"): b for b in card["body"] if b.get("id")}
    channels = {c["title"] for c in inputs["canales"]["choices"]}
    assert channels == {"General", "Reportes"}
    assert inputs["canales"]["value"] == CH_RRHH
    assert {c["value"] for c in inputs["agentes"]["choices"]} == {"asistencia", "liquidaciones"}
    assert inputs["personas"]["choices.data"]["dataset"] == "graph.microsoft.com/users"
    assert card["actions"][0]["data"] == {"accion": SUBMIT_ACTION}


def test_card_is_locked_to_the_team_without_area_choice():
    svc = service()
    card = run(svc.handle_incoming(message(text="solicitar"))).attachment["content"]
    ids = {b.get("id") for b in card["body"]}
    assert "area" not in ids and "nueva_area" not in ids
    team = next(b for b in card["body"] if b["type"] == "FactSet")
    assert team["facts"] == [{"title": "Equipo", "value": "RRHH"}]
    assert "tipo" not in ids  # no se elige tipo: se aplica todo lo marcado
    toggle = next(b for b in card["body"] if b.get("id") == "publicar")
    assert toggle["type"] == "Input.Toggle" and toggle["valueOn"] == "true"
    texts = " ".join(b.get("text", "") for b in card["body"])
    assert "se aplica **todo**" in texts and "sin que nadie escriba antes en Teams" in texts
    assert "a mano a los grupos de Entra" in texts


def test_card_for_team_without_area_has_no_type_choice():
    card = build_request_card("Bodega", [], "", False, [])
    assert not any(b.get("id") == "tipo" for b in card["body"])
    assert any("todavía no tiene agentes" in b.get("text", "") for b in card["body"])


# ---------------- Envío ----------------

def test_submission_is_stored_as_pending_and_changes_nothing():
    svc = service()
    before = svc.areas.items["rrhh"]
    outcome = run(svc.handle_incoming(message(text="hola")))  # canal con área: flujo normal
    assert outcome.status != "request_submitted"
    out = run(svc.submit_request(submit({
        "tipo": "dar_acceso", "area": "rrhh", "personas": f"{P1},{P2.upper()}",
        "agentes": "asistencia", "justificacion": "Equipo de liquidaciones",
    })))
    assert out.status == "request_submitted" and "revisión" in out.user_message
    req = out.request
    assert is_request_id(req.id) and req.status == "pendiente"
    assert req.people == [P1, P2] and req.agents == ["asistencia"]
    assert req.requester_oid == "user-1" and req.requester_name == "Luis"
    assert req.conversation["conversation_id"].startswith(CH_RRHH)
    assert svc.requests.items[req.id] is req
    assert svc.areas.items["rrhh"] is before  # nada se aplicó


@pytest.mark.parametrize(
    "value, expected",
    [
        ({"publicar": "true", "agentes": "asistencia", "justificacion": "x"}, "elige los canales"),
        ({"justificacion": "x"}, "al menos un canal, un agente o una persona"),
        ({"tipo": "agregar_canal", "area": "rrhh", "canales": "19:otro"}, "no pertenecen"),
        ({"tipo": "agregar_canal", "area": "rrhh", "canales": CH_PRIV}, "no pertenecen"),
        ({"tipo": "agregar_agente", "area": "rrhh", "agentes": "fantasma"}, "catálogo"),
        ({"tipo": "dar_acceso", "area": "rrhh", "personas": "luis@icp.cl"}, "lista del formulario"),
        ({"tipo": "dar_acceso", "area": "rrhh", "personas": P1}, "motivo"),
        ({"tipo": "dar_acceso", "area": "rrhh", "justificacion": "x"}, "al menos un canal, un agente o una persona"),
    ],
)
def test_invalid_submissions_are_rejected(value, expected):
    svc = service()
    out = run(svc.submit_request(submit(value)))
    assert out.status == "request_invalid" and expected in out.user_message
    assert svc.requests.items == {}


def test_team_without_area_always_requests_enabling_it():
    data = parse_submission(
        {"tipo": "dar_acceso", "area": "rrhh", "canales": CH_RRHH, "agentes": "asistencia", "justificacion": "x"},
        {CH_RRHH: "General"}, "", {"asistencia"},
    )
    assert data["tipo"] == "nueva_area" and data["area_id"] == ""
    assert data["channels"] == [{"conversationId": CH_RRHH, "name": "General"}]
    with pytest.raises(RequestValidationError, match="al menos un agente"):
        parse_submission({"canales": CH_RRHH, "justificacion": "x"}, {CH_RRHH: "General"}, "", set())


def test_area_comes_from_the_team_not_from_the_card():
    svc = service()
    req = run(svc.submit_request(submit({
        "tipo": "dar_acceso", "area": "contabilidad", "personas": P1, "justificacion": "x"}))).request
    assert req.area_id == "rrhh" and req.nueva_area == ""


def test_new_team_request_uses_team_name():
    svc = service()
    msg = replace(submit({"canales": "19:otro", "agentes": "asistencia", "justificacion": "x"}),
                  channel_conversation_id="19:otro", team_id="team-2", team_name="Bodega")
    req = run(svc.submit_request(msg)).request
    assert req.tipo == "nueva_area" and req.area_id == "" and req.nueva_area == "Bodega"


def test_parse_rejects_too_long_text():
    with pytest.raises(RequestValidationError, match="1000"):
        parse_submission({"tipo": "dar_acceso", "personas": P1, "justificacion": "x" * 1001},
                         {}, "rrhh", set())


def test_request_entity_roundtrip():
    svc = service()
    req = run(svc.submit_request(submit({
        "tipo": "agregar_canal", "area": "rrhh", "canales": CH_RRHH_REPORTES, "justificacion": "Reportes"}))).request
    assert request_from_entity(request_to_entity(req)) == req


def test_request_ids_are_unique_and_formatted():
    ids = {new_request_id() for _ in range(50)}
    assert len(ids) == 50 and all(is_request_id(i) for i in ids)


# ---------------- Revisión ----------------

def test_review_updates_status_but_applies_nothing():
    svc = service()
    req = run(svc.submit_request(submit({
        "tipo": "dar_acceso", "area": "rrhh", "personas": P1, "justificacion": "x"}))).request
    reviewed = run(svc.review_request(req.id, "Aprobada", "Grupo asignado por TI", "admin@icp"))
    assert reviewed.status == "aprobada" and reviewed.reviewed_by == "admin@icp"
    for bad in ("pendiente", "otra"):
        with pytest.raises(RequestDenied):
            run(svc.review_request(req.id, bad, "", "admin"))
    with pytest.raises(RequestDenied) as missing:
        run(svc.review_request("SOL-20260101-XXXX", "rechazada", "", "admin"))
    assert missing.value.status_code == 404


def test_request_endpoints_and_requester_notification():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from admin_api import build_admin_router
    from test_admin import validator

    sent = []

    async def notify(conversation_id, text):
        sent.append((conversation_id, text))

    svc = service()
    req = run(svc.submit_request(submit({
        "tipo": "dar_acceso", "area": "rrhh", "personas": P1, "justificacion": "x"}))).request
    app = FastAPI()
    app.include_router(build_admin_router(svc, validator(), notify=notify))
    c = TestClient(app)

    assert c.get("/api/admin/requests").status_code == 401
    assert [r["id"] for r in c.get("/api/admin/requests?status=pendiente", headers=AUTH).json()["requests"]] == [req.id]
    assert c.get(f"/api/admin/requests/{req.id}", headers=AUTH).json()["people"] == [P1]
    r = c.post(f"/api/admin/requests/{req.id}/review", headers=AUTH, json={"status": "rechazada", "comment": "Falta jefatura"})
    assert r.status_code == 200 and r.json()["notified"] is True
    assert sent[0][0].startswith(CH_RRHH) and "rechazada" in sent[0][1] and "Falta jefatura" in sent[0][1]
    assert c.get("/api/admin/requests?status=pendiente", headers=AUTH).json()["requests"] == []
    assert c.post(f"/api/admin/requests/{req.id}/review", headers=AUTH, json={"status": "x"}).status_code == 400


def test_solicitar_is_reserved_alias():
    from areas import RESERVED_AGENT_ALIASES
    assert "solicitar" in RESERVED_AGENT_ALIASES


def test_double_submit_does_not_duplicate():
    svc = service()
    value = {"tipo": "dar_acceso", "area": "rrhh", "personas": P1, "justificacion": "x"}
    first = run(svc.submit_request(submit(value)))
    second = run(svc.submit_request(submit(value)))
    assert second.status == "request_duplicate" and first.request.id in second.user_message
    assert len(svc.requests.items) == 1
    other = run(svc.submit_request(submit({**value, "justificacion": "otra"})))
    assert other.status == "request_submitted" and len(svc.requests.items) == 2


def test_team_name_is_filled_from_known_channels():
    svc = service()
    msg = replace(submit({"tipo": "dar_acceso", "area": "rrhh", "personas": P1, "justificacion": "x"}), team_name="")
    assert run(svc.submit_request(msg)).request.team_name == "RRHH"
