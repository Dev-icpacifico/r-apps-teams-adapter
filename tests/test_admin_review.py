"""Revisión de solicitudes por chat personal de administradores."""

import asyncio
from dataclasses import replace

from admin_review import REVIEW_ACTION, AdminChat, build_review_card
from conftest import CH_RRHH, CH_RRHH_REPORTES
from stores import KnownChannel
from test_access_requests import P1, service, submit
from test_area_service import message

ADMIN_GROUP = "eeeeeeee-0000-0000-0000-000000000001"


def run(coro):
    return asyncio.run(coro)


class FakeAdminChats:
    def __init__(self):
        self.items = {}

    async def save(self, chat):
        self.items[chat.aad_object_id] = chat

    async def list(self):
        return list(self.items.values())


def admin_service(admins=("admin-1",)):
    svc = service()
    svc.admin_group = ADMIN_GROUP
    svc.admin_chats = FakeAdminChats()
    for a in admins:
        svc.groups.membership[a] = [ADMIN_GROUP]

    async def group_names(ids):
        return {g: f"grupo-{g[:4]}" for g in ids}

    svc.groups.group_names = group_names
    return svc


def personal(user, text="hola", value=None, name="Vicente"):
    return replace(message(channel=None, user=user, text=text), user_name=name, value=value)


def pending_request(svc):
    return run(svc.submit_request(submit({
        "tipo": "dar_acceso", "area": "rrhh", "personas": P1, "agentes": "liquidaciones",
        "justificacion": "Equipo de liquidaciones"}))).request


def test_only_admins_register_their_chat():
    svc = admin_service()
    assert "solo para administradores" in run(svc.register_admin_chat(personal("user-x")))
    assert svc.admin_chats.items == {}
    assert "recibirás aquí" in run(svc.register_admin_chat(personal("admin-1")))
    chat = svc.admin_chats.items["admin-1"]
    assert chat.conversation_id == "a:chat-personal" and chat.name == "Vicente"


def test_admin_targets_drop_people_removed_from_group():
    svc = admin_service(admins=("admin-1", "admin-2"))
    run(svc.register_admin_chat(personal("admin-1")))
    run(svc.register_admin_chat(personal("admin-2")))
    svc.groups.membership["admin-2"] = []
    assert [c.aad_object_id for c in run(svc.admin_targets())] == ["admin-1"]


TEAM_GROUP = "dddddddd-0000-0000-0000-000000000002"
CH_BOD = "19:bodega-general@thread.tacv2"
CH_BOD_2 = "19:bodega-a2@thread.tacv2"


def review(svc, req, estado="aprobada", user="admin-1", comentario=""):
    value = {"accion": REVIEW_ACTION, "id": req.id, "estado": estado, "comentario": comentario}
    return run(svc.review_from_card(personal(user, value=value)))


def bodega_request(svc, agents="asistencia,liquidaciones", people=""):
    for cid, name in ((CH_BOD, "General"), (CH_BOD_2, "a2")):
        run(svc.channels.record(KnownChannel(cid, name, "team-bod", "Bodega (test)", TEAM_GROUP, "https://smba", "x", "standard")))
    msg = replace(
        personal("user-1", value={"accion": "solicitud_agentes", "canales": f"{CH_BOD},{CH_BOD_2}",
                                  "agentes": agents, "personas": people, "justificacion": "Movimientos"}),
        channel_conversation_id=CH_BOD, conversation_id=f"{CH_BOD};messageid=1", team_id="team-bod", team_name="Bodega (test)",
    )
    return run(svc.submit_request(msg)).request


def test_review_card_shows_people_groups_and_only_approve_reject():
    svc = admin_service()
    req = pending_request(svc)
    card = run(svc.review_card(req))
    texts = " ".join(b.get("text", "") for b in card["body"])
    people = next(b for b in card["body"] if b.get("id") == "personas_solicitadas")
    assert people["value"] == P1 and people["choices.data"]["dataset"] == "graph.microsoft.com/users"
    assert "en Entra" in texts and "grupo-" in texts
    assert "Agregar los agentes liquidaciones" not in texts  # dar_acceso no aplica agentes
    facts = {f["title"]: f["value"] for f in card["body"][2]["facts"]}
    assert "Área" not in facts and facts["Equipo"] == "RRHH"
    assert {a["data"]["estado"] for a in card["actions"]} == {"aprobada", "rechazada"}
    assert all(a["data"]["accion"] == REVIEW_ACTION and a["data"]["id"] == req.id for a in card["actions"])


def test_review_from_card_requires_admin():
    svc = admin_service()
    req = pending_request(svc)
    result = review(svc, req, user="user-x")
    assert result.request is None and "Solo los administradores" in result.message and req.status == "pendiente"
    bad = review(svc, req, estado="aplicada")
    assert bad.request is None and not bad.changed


def test_access_request_is_approved_but_entra_stays_manual():
    svc = admin_service()
    req = pending_request(svc)
    before = svc.areas.items["rrhh"]
    result = review(svc, req, comentario="Ok")
    assert result.changed and result.request.status == "aprobada" and result.request.review_comment == "Ok"
    assert result.request.reviewed_by == "Vicente" and "Entra" in result.message
    assert svc.areas.items["rrhh"] is before


def test_resolved_request_cannot_be_reviewed_again():
    svc = admin_service(admins=("admin-1", "admin-2"))
    req = pending_request(svc)
    assert review(svc, req, estado="rechazada").changed
    again = review(svc, req, estado="aprobada", user="admin-2")
    assert not again.changed and "ya fue revisada" in again.message and again.request.status == "rechazada"


def test_resolved_card_has_no_buttons_or_comment():
    svc = admin_service()
    req = pending_request(svc)
    review(svc, req, estado="rechazada", comentario="Falta jefatura")
    card = run(svc.review_card(req))
    assert "actions" not in card and not any(b.get("id") == "comentario" for b in card["body"])
    facts = {f["title"]: f["value"] for f in card["body"][2]["facts"]}
    assert facts["Estado"] == "rechazada" and facts["Revisada por"] == "Vicente" and facts["Comentario"] == "Falta jefatura"


def test_admin_card_refs_are_recorded_once():
    svc = admin_service()
    req = pending_request(svc)
    ref = {"conversationId": "a:1", "activityId": "act-1", "serviceUrl": "https://smba"}
    run(svc.record_admin_card(req.id, ref))
    run(svc.record_admin_card(req.id, ref))
    run(svc.record_admin_card(req.id, {**ref, "activityId": ""}))
    assert svc.requests.items[req.id].admin_cards == [ref]


def test_approving_new_team_creates_the_area():
    svc = admin_service()
    req = bodega_request(svc)
    assert req.tipo == "nueva_area" and req.nueva_area == "Bodega (test)"
    card = run(svc.review_card(req))
    assert any("Habilitar el equipo" in b.get("text", "") for b in card["body"])

    result = review(svc, req)
    assert result.changed and result.request.status == "aplicada", result.message
    area = svc.areas.items["bodega-test"]
    assert area.display_name == "Bodega (test)" and area.required_groups == (TEAM_GROUP,)
    assert {c.alias for c in area.channels} == {"bodega_test_general", "bodega_test_a2"}
    assert set(area.agent_refs) == {"asistencia", "liquidaciones"}
    assert "bodega-test" in result.request.applied[0]
    # El equipo ya tiene área: el formulario pasa a pedir agentes/canales/acceso.
    assert run(svc.area_for_team("team-bod")).id == "bodega-test"


def test_new_team_with_people_stays_approved_until_entra():
    svc = admin_service()
    req = bodega_request(svc, people=P1)
    result = review(svc, req)
    assert result.request.status == "aprobada" and "bodega-test" in svc.areas.items


def test_new_team_approval_fails_cleanly_without_team_group():
    svc = admin_service()
    req = bodega_request(svc)
    svc.channels.recorded = [replace(c, team_aad_group_id="") for c in svc.channels.recorded]
    result = review(svc, req)
    assert not result.changed and "sigue pendiente" in result.message and "Sync-AgentesEquipos" in result.message
    assert svc.requests.items[req.id].status == "pendiente" and "bodega-test" not in svc.areas.items


def test_approving_adds_channels_and_agents_to_team_area():
    svc = admin_service()
    for cid, name in ((CH_RRHH, "General"), (CH_RRHH_REPORTES, "Reportes")):
        run(svc.channels.record(KnownChannel(cid, name, "team-1", "RRHH", "", "https://smba", "x", "standard")))
    run(svc.channels.record(KnownChannel("19:rrhh-nuevo@thread.tacv2", "Nuevo", "team-1", "RRHH", "", "https://smba", "x", "standard")))
    req = run(svc.submit_request(submit({
        "tipo": "agregar_canal", "canales": f"19:rrhh-nuevo@thread.tacv2,{CH_RRHH}",
        "agentes": "liquidaciones", "justificacion": "x"}))).request
    result = review(svc, req)
    assert result.request.status == "aplicada", result.message
    area = svc.areas.items["rrhh"]
    assert "rrhh_nuevo" in {c.alias for c in area.channels} and "liquidaciones" in area.agent_refs
    assert len(result.request.applied) == 2


def test_without_admin_group_nobody_is_admin():
    svc = service()
    assert run(svc.is_admin("admin-1")) is False
    assert "no está configurada" in run(svc.register_admin_chat(personal("admin-1")))
