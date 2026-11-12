import pytest

from areas import (
    AreaValidationError,
    area_from_entity,
    area_to_entity,
    ensure_unique_across_areas,
    parse_area_payload,
)
from conftest import CH_CONT, CH_RRHH, G_ASISS, G_BOD, G_FIN, G_LIQUID, TRIGGER_B, agent, make_area
from policy import evaluate_access


# ---------------- Evaluación any / all ----------------

def test_any_allows_with_one_group():
    d = evaluate_access("any", [G_LIQUID, G_ASISS], [G_ASISS, "otro"])
    assert d.allowed and d.matched_groups == (G_ASISS,)


def test_any_denies_without_groups():
    assert not evaluate_access("any", [G_LIQUID], ["otro"]).allowed


def test_all_requires_every_group():
    assert evaluate_access("all", [G_FIN, G_BOD], [G_FIN, G_BOD]).allowed
    partial = evaluate_access("all", [G_FIN, G_BOD], [G_FIN])
    assert not partial.allowed and "1 de 2" in partial.reason


def test_comparison_is_case_insensitive():
    assert evaluate_access("any", [G_LIQUID.upper()], [G_LIQUID]).allowed


def test_no_required_groups_denies():
    assert not evaluate_access("any", [], [G_LIQUID]).allowed


def test_unknown_mode_denies():
    assert not evaluate_access("some", [G_LIQUID], [G_LIQUID]).allowed


# ---------------- Validación de payload ----------------

def valid_payload(**overrides):
    payload = {
        "displayName": "Recursos Humanos",
        "accessMode": "any",
        "requiredGroups": [G_LIQUID, G_ASISS],
        "channels": [{"alias": "rrhh_general", "conversationId": CH_RRHH, "displayName": "General"}],
        "agents": [{"alias": "asistencia", "displayName": "Asistencia", "triggerId": "agtch_aaaaaaaaaaaaaaaa"}],
    }
    payload.update(overrides)
    return payload


def test_valid_payload_parses():
    area = parse_area_payload("rrhh", valid_payload(), updated_by="admin@x")
    assert area.id == "rrhh"
    assert area.required_groups == (G_LIQUID, G_ASISS)
    assert area.enabled and not area.allow_publish_without_request
    assert area.updated_by == "admin@x"


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"accessMode": "some"}, "accessMode"),
        ({"requiredGroups": []}, "requiredGroups"),
        ({"requiredGroups": ["Buk Liquid"]}, "grupo inválido"),
        ({"channels": []}, "channels"),
        ({"channels": [{"alias": "RRHH Gen", "conversationId": CH_RRHH}]}, "alias inválido"),
        ({"channels": [{"alias": "rrhh", "conversationId": "https://teams/x"}]}, "conversationId"),
        (
            {"channels": [
                {"alias": "a_1", "conversationId": CH_RRHH},
                {"alias": "a_1", "conversationId": CH_CONT},
            ]},
            "alias repetido",
        ),
        ({"agents": []}, "agents debe incluir"),
        ({"agents": [{"alias": "x", "displayName": "X", "triggerId": "agtch_aaaaaaaaaa"}]}, "alias de agente inválido"),
        ({"agents": [{"alias": "agentes", "displayName": "X", "triggerId": "agtch_aaaaaaaaaa"}]}, "reservado"),
        ({"agents": [{"alias": "uny", "displayName": "X", "triggerId": "https://chatgpt"}]}, "triggerId"),
        ({"agents": [{"alias": "uny", "displayName": "", "triggerId": "agtch_aaaaaaaaaa"}]}, "displayName obligatorio"),
        ({"agents": [{"alias": "uny", "displayName": "U", "triggerId": "agtch_aaaaaaaaaa", "tokenSecret": "a b"}]},
         "tokenSecret"),
        (
            {"agents": [
                {"alias": "uny", "displayName": "U", "triggerId": "agtch_aaaaaaaaaa"},
                {"alias": "uny", "displayName": "U2", "triggerId": "agtch_bbbbbbbbbb"},
            ]},
            "alias de agente repetido",
        ),
        ({"defaultAgent": "otro"}, "defaultAgent"),
        ({"enabled": "yes"}, "enabled"),
    ],
)
def test_invalid_payloads(overrides, expected):
    with pytest.raises(AreaValidationError, match=expected):
        parse_area_payload("rrhh", valid_payload(**overrides))


def test_invalid_area_id():
    with pytest.raises(AreaValidationError, match="id de área"):
        parse_area_payload("RRHH", valid_payload())


def test_duplicate_groups_are_collapsed():
    area = parse_area_payload("rrhh", valid_payload(requiredGroups=[G_LIQUID, G_LIQUID.upper()]))
    assert area.required_groups == (G_LIQUID,)


def test_channel_cannot_belong_to_two_areas():
    rrhh = make_area()
    other = make_area("ti", channels=(("ti_general", CH_RRHH),))
    with pytest.raises(AreaValidationError, match="ya pertenece al área 'rrhh'"):
        ensure_unique_across_areas(other, [rrhh])


def test_alias_cannot_repeat_across_areas():
    rrhh = make_area()
    other = make_area("ti", channels=(("rrhh_general", CH_CONT),))
    with pytest.raises(AreaValidationError, match="alias 'rrhh_general'"):
        ensure_unique_across_areas(other, [rrhh])


def test_updating_same_area_is_allowed():
    rrhh = make_area()
    ensure_unique_across_areas(make_area(groups=(G_LIQUID,)), [rrhh])


def test_entity_roundtrip():
    area = make_area("contabilidad", mode="all", groups=(G_FIN, G_BOD), channels=(("cont", CH_CONT),),
                     agents=(agent("uny"), agent("ads", TRIGGER_B, "token-ads")), default_agent="uny")
    assert area_from_entity(area_to_entity(area)) == area


def test_legacy_agent_block_is_rejected_with_hint():
    payload = valid_payload()
    del payload["agents"]
    payload["agent"] = {"triggerUrlSecret": "a", "tokenSecret": "b"}
    with pytest.raises(AreaValidationError, match="reemplazado por agents"):
        parse_area_payload("rrhh", payload)


def test_default_agent_and_description_are_parsed():
    area = parse_area_payload("rrhh", valid_payload(
        agents=[{"alias": "Uny-Agent", "displayName": "UnyAgent", "description": "Datos Unysoft",
                 "triggerId": "agtch_aaaaaaaaaaaaaaaa"}],
        defaultAgent="uny-agent",
    ))
    assert area.agents[0].alias == "uny-agent" and area.default_agent == "uny-agent"
    assert area.agents[0].description == "Datos Unysoft" and area.agents[0].token_secret == ""


def test_corrupt_entity_is_rejected():
    entity = area_to_entity(make_area())
    entity["access_mode"] = "some"
    with pytest.raises(AreaValidationError):
        area_from_entity(entity)
