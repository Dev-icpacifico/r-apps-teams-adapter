"""Reglas de grupos por agente (motor de permisos por área y agente)."""

import asyncio

import pytest

from area_service import route_message
from denials import HOW_TO_REQUEST
from dataclasses import replace

from areas import AreaAgent, AreaValidationError, area_from_entity, area_to_entity, parse_area_payload
from conftest import CH_RRHH, G_ASISS, G_BOD, G_FIN, G_GENERAL, G_LIQUID, TRIGGER_A, TRIGGER_B, make_area
from policy import evaluate_agent_access
from test_area_service import FakeAgents, FakeGroups, build_service, message
from test_policy_and_areas import valid_payload

G_VAMBE = "66666666-6666-6666-6666-666666666666"
G_META = "77777777-7777-7777-7777-777777777777"
G_UNY_GER = "88888888-8888-8888-8888-888888888888"
G_UNY_FIN = "99999999-9999-9999-9999-999999999999"
TRIGGER_C = "agtch_cccccccccccccccccccc"
TRIGGER_D = "agtch_dddddddddddddddddddd"


def run(coro):
    return asyncio.run(coro)


def ruled(alias, trigger, groups, mode="any"):
    return AreaAgent(
        alias, alias.capitalize(), trigger, f"Agente {alias}",
        access_mode=mode, required_groups=tuple(groups),
    )


def area_por_agente(area_groups=(), default_agent=""):
    """Área sin regla común: cada agente exige su propio grupo."""
    return make_area(
        "td",
        groups=area_groups,
        channels=(("td_mensajes", CH_RRHH),),
        agents=[
            ruled("vambe", TRIGGER_A, [G_VAMBE]),
            ruled("metaads", TRIGGER_B, [G_META]),
            ruled("unyagent", TRIGGER_C, [G_UNY_GER, G_UNY_FIN]),
            ruled("finbod", TRIGGER_D, [G_FIN, G_BOD], mode="all"),
        ],
        default_agent=default_agent,
    )


def service_for(groups_of_user, area=None, agents=None):
    return build_service(
        areas=[area or area_por_agente()],
        membership={"user-1": list(groups_of_user)},
        agents=agents,
    )


# ---------------- Evaluación ----------------

def test_agent_rule_alone():
    area = area_por_agente()
    vambe, meta = area.agents[0], area.agents[1]
    assert evaluate_agent_access(area, vambe, [G_VAMBE]).allowed
    denied = evaluate_agent_access(area, meta, [G_VAMBE])
    assert not denied.allowed and "metaads" in denied.reason


def test_area_rule_and_agent_rule_both_required():
    area = area_por_agente(area_groups=(G_GENERAL,))
    vambe = area.agents[0]
    assert not evaluate_agent_access(area, vambe, [G_VAMBE]).allowed
    assert not evaluate_agent_access(area, vambe, [G_GENERAL]).allowed
    assert evaluate_agent_access(area, vambe, [G_GENERAL, G_VAMBE]).allowed


def test_agent_without_rule_inherits_area_rule():
    area = make_area(groups=(G_LIQUID,))
    plain = area.agents[0]
    assert evaluate_agent_access(area, plain, [G_LIQUID]).allowed
    assert not evaluate_agent_access(area, plain, [G_ASISS]).allowed


def test_agent_without_any_rule_is_denied():
    area = make_area(groups=())
    assert not evaluate_agent_access(area, area.agents[0], [G_LIQUID]).allowed


def test_agent_all_mode():
    area = area_por_agente()
    finbod = area.agents[3]
    assert not evaluate_agent_access(area, finbod, [G_FIN]).allowed
    assert evaluate_agent_access(area, finbod, [G_FIN, G_BOD]).allowed


# ---------------- Entrada desde Teams ----------------

def test_user_only_in_vambe_can_use_vambe():
    agents = FakeAgents()
    service = service_for([G_VAMBE], agents=agents)
    outcome = run(service.handle_incoming(message(text="vambe leads de octubre")))
    assert outcome.status == "accepted" and outcome.agent_alias == "vambe"
    assert agents.calls[0][1] == "leads de octubre"


def test_user_only_in_vambe_cannot_use_unyagent():
    agents = FakeAgents()
    service = service_for([G_VAMBE], agents=agents)
    outcome = run(service.handle_incoming(message(text="unyagent pedidos de septiembre")))
    assert outcome.status == "agent_forbidden" and outcome.agent_alias == "unyagent"
    assert "No tienes permisos para usar **Unyagent**" in outcome.user_message
    assert "**vambe**" in outcome.user_message and "metaads" not in outcome.user_message
    assert agents.calls == [] and service.correlations.items == {}


def test_second_group_unlocks_second_agent():
    service = service_for([G_VAMBE, G_META])
    assert run(service.handle_incoming(message(text="metaads gasto"))).status == "accepted"
    assert run(service.handle_incoming(message(text="vambe leads"))).status == "accepted"
    assert run(service.handle_incoming(message(text="unyagent ventas"))).status == "agent_forbidden"


def test_any_of_several_agent_groups_is_enough():
    service = service_for([G_UNY_FIN])
    assert run(service.handle_incoming(message(text="unyagent ventas"))).status == "accepted"


def test_agent_list_shows_only_allowed_agents():
    service = service_for([G_VAMBE, G_META])
    outcome = run(service.handle_incoming(message(text="agentes")))
    assert outcome.status == "agents_list"
    assert "**vambe**" in outcome.user_message and "**metaads**" in outcome.user_message
    assert "unyagent" not in outcome.user_message and "finbod" not in outcome.user_message


def test_message_without_alias_goes_to_only_allowed_agent():
    agents = FakeAgents()
    service = service_for([G_VAMBE], agents=agents)
    outcome = run(service.handle_incoming(message(text="leads de octubre")))
    assert outcome.status == "accepted" and outcome.agent_alias == "vambe"
    assert agents.calls[0][1] == "leads de octubre"


def test_message_without_alias_asks_when_several_allowed():
    service = service_for([G_VAMBE, G_META])
    outcome = run(service.handle_incoming(message(text="leads de octubre")))
    assert outcome.status == "choose_agent" and "unyagent" not in outcome.user_message


def test_default_agent_not_allowed_falls_back_to_only_allowed():
    area = area_por_agente(default_agent="unyagent")
    service = service_for([G_VAMBE], area=area)
    outcome = run(service.handle_incoming(message(text="leads de octubre")))
    assert outcome.status == "accepted" and outcome.agent_alias == "vambe"


def test_user_without_agent_groups_is_forbidden():
    agents = FakeAgents()
    service = service_for([G_LIQUID], agents=agents)
    outcome = run(service.handle_incoming(message(text="vambe leads")))
    assert outcome.status == "forbidden" and "necesitas" in outcome.user_message
    assert HOW_TO_REQUEST in outcome.user_message
    assert agents.calls == []


def test_graph_is_queried_once_with_all_rule_groups():
    seen = []

    class RecordingGroups(FakeGroups):
        async def member_groups(self, aad_object_id, group_ids):
            seen.append(list(group_ids))
            return await super().member_groups(aad_object_id, group_ids)

    area = area_por_agente(area_groups=(G_GENERAL,))
    service = build_service(
        areas=[area], groups=RecordingGroups({"user-1": [G_GENERAL, G_VAMBE]})
    )
    assert run(service.handle_incoming(message(text="vambe leads"))).status == "accepted"
    assert len(seen) == 1
    assert set(seen[0]) == {G_GENERAL, G_VAMBE, G_META, G_UNY_GER, G_UNY_FIN, G_FIN, G_BOD}


def test_route_alias_not_allowed_is_still_recognized():
    area = area_por_agente()
    route = route_message(area, "unyagent ventas", allowed=[area.agents[0]])
    assert route.kind == "agent" and route.agent.alias == "unyagent"


# ---------------- Catálogo y áreas ----------------

def test_area_rule_can_be_omitted_in_payload():
    payload = valid_payload(agents=["vambe", "unyagent"])
    del payload["requiredGroups"]
    del payload["accessMode"]
    area = parse_area_payload("td", payload)
    assert area.required_groups == () and area.access_mode == "any"
    assert area.agent_refs == ("vambe", "unyagent")


def test_area_without_rule_needs_restricted_catalog_agents():
    from conftest import CH_RRHH as CH
    from test_area_service import FakeCatalog

    catalog = FakeCatalog([ruled("vambe", TRIGGER_A, [G_VAMBE]), AreaAgent("abierto", "Abierto", TRIGGER_B)])
    svc = build_service(areas=[], known=(CH,), catalog=catalog)
    body = {"displayName": "TD", "channels": [{"alias": "td_x", "conversationId": CH}], "agents": ["vambe", "abierto"]}
    with pytest.raises(AreaValidationError, match="no exigen grupos propios.*abierto"):
        run(svc.upsert_area("td", body, "admin"))
    area = run(svc.upsert_area("td", {**body, "requiredGroups": [G_LIQUID]}, "admin"))
    assert [a.alias for a in area.agents] == ["vambe", "abierto"]


def test_agent_rule_travels_with_the_agent_to_every_area():
    """Regla del usuario: el grupo del agente rige en cualquier área."""
    from test_area_service import FakeCatalog

    catalog = FakeCatalog([ruled("unyagent", TRIGGER_C, [G_UNY_GER, G_UNY_FIN]), AreaAgent("noticias", "Noticias", TRIGGER_D)])
    finanzas = replace(
        make_area("finanzas", groups=(G_LIQUID,), channels=(("fin_general", CH_RRHH),), agents=()),
        agent_refs=("unyagent", "noticias"),
    )
    svc = build_service(areas=[finanzas], membership={"user-1": [G_LIQUID]}, catalog=catalog)
    listing = run(svc.handle_incoming(message(text="agentes")))
    assert "**noticias**" in listing.user_message and "unyagent" not in listing.user_message
    assert run(svc.handle_incoming(message(text="unyagent ventas"))).status == "agent_forbidden"

    lector = build_service(areas=[finanzas], membership={"user-1": [G_LIQUID, G_UNY_FIN]}, catalog=catalog)
    assert run(lector.handle_incoming(message(text="unyagent ventas"))).status == "accepted"


def test_unknown_catalog_reference_is_not_offered():
    from test_area_service import FakeCatalog

    area = replace(make_area(groups=(G_LIQUID,), agents=()), agent_refs=("fantasma",))
    svc = build_service(areas=[area], membership={"user-1": [G_LIQUID]}, catalog=FakeCatalog([]))
    assert run(svc.handle_incoming(message(text="fantasma hola"))).status == "forbidden"


def test_legacy_entity_keeps_only_aliases():
    entity = area_to_entity(make_area())
    entity.pop("agent_refs")
    entity["agents"] = (
        '[{"alias": "asistencia", "display_name": "A", "trigger_id": "agtch_aaaaaaaaaaaaaaaa", '
        '"required_groups": []}]'
    )
    area = area_from_entity(entity)
    assert area.agent_refs == ("asistencia",) and area.agents == ()
