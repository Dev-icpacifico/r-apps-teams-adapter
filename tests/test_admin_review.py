"""Revisión de solicitudes por chat personal de administradores."""

import asyncio

import pytest
from dataclasses import replace

from admin_review import REVIEW_ACTION, AdminChat, build_review_card
from conftest import CH_RRHH, CH_RRHH_REPORTES
from stores import KnownChannel
from test_access_requests import P1, service, submit
from test_area_service import message

ADMIN_GROUP = "eeeeeeee-0000-0000-0000-000000000001"
AREA_GROUP = "a4c7379d-0000-0000-0000-000000000004"


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

    async def find_group(value):
        known = {"r-grpag-teamsbodtest": (AREA_GROUP, "r-grpAg-TeamsBodTEST"), AREA_GROUP: (AREA_GROUP, "r-grpAg-TeamsBodTEST")}
        return known.get(value.strip().lower())

    svc.groups.group_names = group_names
    svc.groups.find_group = find_group
    return svc


def personal(user, text="hola", value=None, name="Vicente"):
    return replace(message(channel=None, user=user, text=text), user_name=name, value=value)


def pending_request(svc):
    return run(svc.submit_request(submit({
        "area": "rrhh", "personas": P1, "justificacion": "Equipo de liquidaciones"}))).request


def test_only_admins_register_their_chat():
    svc = admin_service()
    notice = run(svc.register_admin_chat(personal("user-x")))
    assert notice.startswith("Hola, Vicente.") and "solo para recibir noticias" in notice
    assert "no llega a ningún agente" in notice and "solicitar" in notice
    assert svc.admin_chats.items == {}
    assert "Recibirás aquí" in run(svc.register_admin_chat(personal("admin-1")))
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


def review(svc, req, estado="aprobada", user="admin-1", comentario="", grupo=None, publicadores=None):
    value = {"accion": REVIEW_ACTION, "id": req.id, "estado": estado, "comentario": comentario}
    if grupo is not None:
        value["grupo_area"] = grupo
    if publicadores is not None:
        value["grupo_publicadores"] = publicadores
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
    assert "Agregar los agentes" not in texts  # solo personas: no se aplican agentes
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
    assert any(b.get("id") == "grupo_area" for b in card["body"])

    result = review(svc, req, grupo=" r-grpAg-TeamsBodTEST ")
    assert result.changed and result.request.status == "aplicada", result.message
    area = svc.areas.items["bodega-test"]
    assert area.display_name == "Bodega (test)" and area.required_groups == (AREA_GROUP,)
    assert "r-grpAg-TeamsBodTEST" in result.request.applied[0]
    # Tras resolverla, la tarjeta ya no pide el grupo.
    assert not any(b.get("id") == "grupo_area" for b in run(svc.review_card(result.request))["body"])
    assert {c.alias for c in area.channels} == {"bodega_test_general", "bodega_test_a2"}
    assert set(area.agent_refs) == {"asistencia", "liquidaciones"}
    assert "bodega-test" in result.request.applied[0]
    # El equipo ya tiene área: el formulario pasa a pedir agentes/canales/acceso.
    assert run(svc.area_for_team("team-bod")).id == "bodega-test"


def test_new_team_with_people_stays_approved_until_entra():
    svc = admin_service()
    req = bodega_request(svc, people=P1)
    result = review(svc, req, grupo=AREA_GROUP.upper())
    assert result.request.status == "aprobada" and "bodega-test" in svc.areas.items


@pytest.mark.parametrize("grupo, expected", [(None, "indica el grupo"), ("  ", "indica el grupo"),
                                             ("r-grpAg-Inexistente", "no encontré")])
def test_new_team_approval_needs_an_existing_area_group(grupo, expected):
    svc = admin_service()
    req = bodega_request(svc)
    result = review(svc, req, grupo=grupo)
    assert not result.changed and "sigue pendiente" in result.message and expected in result.message
    assert svc.requests.items[req.id].status == "pendiente" and "bodega-test" not in svc.areas.items
    # Rechazar no exige grupo.
    assert review(svc, req, estado="rechazada").changed


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
    assert result.request.applied == [
        "Canal «Nuevo» agregado (rrhh_nuevo).", "Agente liquidaciones agregado.",
        "El canal «General» ya estaba habilitado.",
    ]


def _rrhh_new_channel(svc):
    for cid, name in ((CH_RRHH, "General"), (CH_RRHH_REPORTES, "Reportes"), ("19:rrhh-nuevo@thread.tacv2", "Nuevo")):
        run(svc.channels.record(KnownChannel(cid, name, "team-1", "RRHH", "", "https://smba", "x", "standard")))


def test_one_request_applies_channel_agent_and_publishing():
    svc = admin_service()
    _rrhh_new_channel(svc)
    svc.areas.items["rrhh"] = replace(svc.areas.items["rrhh"], direct_publish=None)
    req = run(svc.submit_request(submit({
        "canales": "19:rrhh-nuevo@thread.tacv2", "agentes": "liquidaciones", "publicar": "true",
        "justificacion": "Reportes diarios"}))).request
    assert req.tipo == "solicitud" and req.publish
    card = run(svc.review_card(req))
    texts = " ".join(b.get("text", "") for b in card["body"])
    assert "Agregar los canales Nuevo" in texts and "Agregar los agentes liquidaciones" in texts
    assert "Permitir publicar desde ChatGPT en Nuevo" in texts
    assert any(b.get("id") == "grupo_publicadores" for b in card["body"])
    # Sin grupo de publicadores no se aprueba ni se cambia nada.
    missing = review(svc, req)
    assert not missing.changed and "quienes pueden publicar" in missing.message
    assert "rrhh_nuevo" not in {c.alias for c in svc.areas.items["rrhh"].channels}

    result = review(svc, req, publicadores="r-grpAg-TeamsBodTEST")
    assert result.request.status == "aplicada", result.message
    area = svc.areas.items["rrhh"]
    assert "rrhh_nuevo" in {c.alias for c in area.channels} and "liquidaciones" in area.agent_refs
    assert area.direct_publish.channels == ("rrhh_nuevo",) and area.direct_publish.required_groups == (AREA_GROUP,)
    assert any("Publicación desde ChatGPT permitida en rrhh_nuevo" in a for a in result.request.applied)


def test_nothing_new_says_what_was_already_configured():
    svc = admin_service()
    _rrhh_new_channel(svc)
    req = run(svc.submit_request(submit({"canales": CH_RRHH, "justificacion": "x"}))).request
    result = review(svc, req)
    assert result.request.applied == [
        "No había cambios que aplicar: ya estaba todo configurado.",
        "El canal «General» ya estaba habilitado.",
    ]


def test_without_admin_group_nobody_is_admin():
    svc = service()
    assert run(svc.is_admin("admin-1")) is False
    assert "no está configurada" in run(svc.register_admin_chat(personal("admin-1")))
