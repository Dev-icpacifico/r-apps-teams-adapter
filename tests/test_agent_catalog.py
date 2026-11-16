"""Catálogo global de agentes: validación, persistencia, servicio y API."""

import asyncio

import pytest

from agent_catalog import agent_from_entity, agent_to_entity, parse_agent_payload
from area_service import RequestDenied
from areas import AreaValidationError
from conftest import G_FIN, G_LIQUID
from test_admin import client, token
from test_area_service import FakeAgents, FakeCatalog, build_service

G_UNY_GER = "88888888-8888-8888-8888-888888888888"
G_UNY_FIN = "99999999-9999-9999-9999-999999999999"
AUTH = {"Authorization": token()}


def run(coro):
    return asyncio.run(coro)


def body(**overrides):
    data = {
        "displayName": "UnyAgent",
        "description": "Datos de Unysoft",
        "triggerId": "agtch_cccccccccccccccc",
        "accessMode": "any",
        "requiredGroups": [G_UNY_GER, G_UNY_FIN.upper(), G_UNY_GER],
    }
    data.update(overrides)
    return data


def test_parse_agent_with_several_groups():
    agent = parse_agent_payload("UnyAgent", body())
    assert agent.alias == "unyagent" and agent.access_mode == "any"
    assert agent.required_groups == (G_UNY_GER, G_UNY_FIN)


def test_agent_without_groups_is_allowed_in_catalog():
    assert parse_agent_payload("noticias", body(requiredGroups=[])).required_groups == ()


@pytest.mark.parametrize(
    "alias, overrides, expected",
    [
        ("agentes", {}, "reservado"),
        ("x", {}, "alias de agente inválido"),
        ("uny", {"triggerId": "https://chatgpt"}, "triggerId"),
        ("uny", {"displayName": ""}, "displayName"),
        ("uny", {"tokenSecret": "a b"}, "tokenSecret"),
        ("uny", {"accessMode": "some"}, "accessMode"),
        ("uny", {"requiredGroups": "grupo"}, "requiredGroups debe ser una lista"),
        ("uny", {"requiredGroups": ["UnyFin"]}, "grupo inválido"),
    ],
)
def test_invalid_agent_payload(alias, overrides, expected):
    with pytest.raises(AreaValidationError, match=expected):
        parse_agent_payload(alias, body(**overrides))


def test_entity_roundtrip():
    agent = parse_agent_payload("unyagent", body(tokenSecret="token-uny", accessMode="all"))
    assert agent_from_entity(agent_to_entity(agent, "2026-11-16", "admin")) == agent


def test_upsert_checks_token_secret():
    svc = build_service(secrets=("agent-workspace-token",))
    agent = run(svc.upsert_catalog_agent("unyagent", body(), "admin"))
    assert svc.catalog.items["unyagent"] == agent and svc.catalog.saved == [("unyagent", "admin")]
    with pytest.raises(AreaValidationError, match="token-x"):
        run(svc.upsert_catalog_agent("unyagent", body(tokenSecret="token-x"), "admin"))
    sin_default = build_service(secrets=("agent-workspace-token",), agents=FakeAgents(default_token_secret=""))
    with pytest.raises(AreaValidationError, match="no tiene token"):
        run(sin_default.upsert_catalog_agent("unyagent", body(), "admin"))


def test_removing_groups_is_refused_where_area_has_no_rule():
    from dataclasses import replace

    from conftest import make_area

    area = replace(make_area("td", groups=(), agents=()), agent_refs=("vambe",))
    svc = build_service(areas=[area], secrets=("agent-workspace-token",))
    with pytest.raises(AreaValidationError, match="quedaría abierto.*td"):
        run(svc.upsert_catalog_agent("vambe", body(requiredGroups=[]), "admin"))


def test_delete_refused_while_in_use():
    svc = build_service()
    with pytest.raises(RequestDenied) as missing:
        run(svc.delete_catalog_agent("nada", "admin"))
    assert missing.value.status_code == 404

    from dataclasses import replace

    from conftest import make_area

    area = replace(make_area(agents=()), agent_refs=("asistencia",))
    svc = build_service(areas=[area])
    with pytest.raises(RequestDenied) as conflict:
        run(svc.delete_catalog_agent("asistencia", "admin"))
    assert conflict.value.status_code == 409 and "rrhh" in conflict.value.message
    run(svc.delete_catalog_agent("liquidaciones", "admin"))
    assert "liquidaciones" not in svc.catalog.items


def test_catalog_endpoints():
    from dataclasses import replace

    from conftest import make_area

    area = replace(make_area(agents=()), agent_refs=("asistencia",))
    svc = build_service(areas=[area], secrets=("agent-workspace-token",))
    c = client(svc)
    assert c.get("/api/admin/agents").status_code == 401
    listing = c.get("/api/admin/agents", headers=AUTH).json()["agents"]
    assert {a["alias"]: a["usedBy"] for a in listing} == {"asistencia": ["rrhh"], "liquidaciones": []}

    put = c.put("/api/admin/agents/unyagent", headers=AUTH, json=body(requiredGroups=[G_FIN, G_LIQUID]))
    assert put.status_code == 200 and put.json()["requiredGroups"] == [G_FIN, G_LIQUID]
    assert c.put("/api/admin/agents/unyagent", headers=AUTH, json=body(triggerId="x")).status_code == 400
    assert c.get("/api/admin/agents/unyagent", headers=AUTH).json()["usedBy"] == []
    assert c.get("/api/admin/agents/nada", headers=AUTH).status_code == 404

    assert c.delete("/api/admin/agents/asistencia", headers=AUTH).status_code == 409
    assert c.delete("/api/admin/agents/unyagent", headers=AUTH).status_code == 200

    detail = c.get("/api/admin/areas/rrhh", headers=AUTH).json()
    assert detail["agents"] == ["asistencia"] and detail["agentDetails"][0]["alias"] == "asistencia"
