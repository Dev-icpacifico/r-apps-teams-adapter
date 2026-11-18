"""Revisión de solicitudes por chat personal de administradores."""

import asyncio
from dataclasses import replace

from admin_review import REVIEW_ACTION, AdminChat, build_review_card, next_steps_for
from conftest import CH_RRHH
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


def test_review_card_shows_people_groups_and_manual_steps():
    svc = admin_service()
    req = pending_request(svc)
    card = run(svc.review_card(req))
    texts = " ".join(b.get("text", "") for b in card["body"])
    component = next(b for b in card["body"] if b.get("type") == "Component")
    assert component["properties"]["users"] == [{"id": P1}]
    assert "asignación manual en Entra" in texts and "grupo-" in texts
    assert "Add-AgentesAgenteArea -Area rrhh -Alias liquidaciones" in texts
    assert {a["data"]["estado"] for a in card["actions"]} == {"aprobada", "rechazada", "aplicada"}
    assert all(a["data"]["accion"] == REVIEW_ACTION and a["data"]["id"] == req.id for a in card["actions"])


def test_review_from_card_requires_admin_and_changes_only_status():
    svc = admin_service()
    req = pending_request(svc)
    before = svc.areas.items["rrhh"]
    value = {"accion": REVIEW_ACTION, "id": req.id, "estado": "aprobada", "comentario": "Ok"}

    text, none = run(svc.review_from_card(personal("user-x", value=value)))
    assert none is None and "Solo los administradores" in text and req.status == "pendiente"

    text, reviewed = run(svc.review_from_card(personal("admin-1", value=value)))
    assert reviewed.status == "aprobada" and reviewed.review_comment == "Ok" and reviewed.reviewed_by == "Vicente"
    assert "aprobada" in text and svc.areas.items["rrhh"] is before

    text, bad = run(svc.review_from_card(personal("admin-1", value={**value, "estado": "pendiente"})))
    assert bad is None and "No pude registrar" in text


def test_without_admin_group_nobody_is_admin():
    svc = service()
    assert run(svc.is_admin("admin-1")) is False
    assert "no está configurada" in run(svc.register_admin_chat(personal("admin-1")))


def test_next_steps_for_new_area():
    svc = admin_service()
    req = run(svc.submit_request(submit({
        "tipo": "nueva_area", "area": "__nueva__", "nueva_area": "Bodega", "canales": CH_RRHH,
        "agentes": "asistencia", "justificacion": "x"}))).request
    steps = next_steps_for(req)
    assert "New-AgentesArea" in steps[0] and "'General'" in steps[0] and "-Equipo 'RRHH'" in steps[0]
    card = build_review_card(req, ["grupo del área nueva (si se define)"], steps)
    assert not any(b.get("type") == "Component" for b in card["body"])
