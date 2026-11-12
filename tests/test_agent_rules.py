"""Reglas de grupos por agente (motor de permisos por área y agente)."""

import asyncio

import pytest

from area_service import MSG_FORBIDDEN, route_message
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
    assert outcome.status == "forbidden" and outcome.user_message == MSG_FORBIDDEN
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


# ---------------- Payload y persistencia ----------------

def agents_payload():
    return [
        {"alias": "vambe", "displayName": "Vambe", "triggerId": "agtch_aaaaaaaaaaaaaaaa",
         "requiredGroups": [G_VAMBE.upper()]},
        {"alias": "unyagent", "displayName": "UnyAgent", "triggerId": "agtch_bbbbbbbbbbbbbbbb",
         "accessMode": "all", "requiredGroups": [G_UNY_GER, G_UNY_FIN, G_UNY_GER]},
    ]


def test_payload_with_agent_rules_and_no_area_rule():
    area = parse_area_payload("td", valid_payload(requiredGroups=[], agents=agents_payload()))
    assert area.required_groups == ()
    assert area.agents[0].required_groups == (G_VAMBE,) and area.agents[0].access_mode == "any"
    assert area.agents[1].required_groups == (G_UNY_GER, G_UNY_FIN)
    assert area.agents[1].access_mode == "all"
    public = area.to_public_dict()["agents"][1]
    assert public["accessMode"] == "all" and public["requiredGroups"] == [G_UNY_GER, G_UNY_FIN]


def test_payload_without_area_groups_needs_rule_on_every_agent():
    agents = agents_payload() + [
        {"alias": "abierto", "displayName": "Abierto", "triggerId": "agtch_cccccccccccccccc"}
    ]
    with pytest.raises(AreaValidationError, match="faltan: abierto"):
        parse_area_payload("td", valid_payload(requiredGroups=[], agents=agents))


def test_payload_area_requiredgroups_can_be_omitted():
    payload = valid_payload(agents=agents_payload())
    del payload["requiredGroups"]
    del payload["accessMode"]
    area = parse_area_payload("td", payload)
    assert area.required_groups == () and area.access_mode == "any"


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"requiredGroups": ["Vambe"]}, "grupo inválido del agente 'vambe'"),
        ({"requiredGroups": G_VAMBE}, "requiredGroups del agente 'vambe'"),
        ({"accessMode": "some"}, "accessMode del agente 'vambe'"),
    ],
)
def test_invalid_agent_rule(override, expected):
    agents = agents_payload()
    agents[0].update(override)
    with pytest.raises(AreaValidationError, match=expected):
        parse_area_payload("td", valid_payload(agents=agents))


def test_entity_roundtrip_keeps_agent_rules():
    area = area_por_agente()
    restored = area_from_entity(area_to_entity(area))
    assert restored.required_groups == ()
    assert restored.agents[2].required_groups == (G_UNY_GER, G_UNY_FIN)
    assert restored.agents[3].access_mode == "all"


def test_legacy_entity_without_agent_rules_still_loads():
    entity = area_to_entity(make_area())
    entity["agents"] = (
        '[{"alias": "asistencia", "display_name": "A", "trigger_id": "agtch_aaaaaaaaaaaaaaaa"}]'
    )
    area = area_from_entity(entity)
    assert area.agents[0].required_groups == () and area.agents[0].access_mode == "any"
    assert evaluate_agent_access(area, area.agents[0], [G_LIQUID]).allowed
