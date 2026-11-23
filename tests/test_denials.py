"""Cada rechazo explica qué falta y cómo pedirlo (pedido del usuario 23/11)."""

import asyncio
from dataclasses import replace

import pytest

from area_service import RequestDenied
from areas import DirectPublishRule
from conftest import G_ASISS, G_LIQUID, make_area
from denials import HOW_TO_REQUEST, RETRY_LOOKUP, describe_requirements, unmet_rule
from test_agent_rules import G_VAMBE, service_for
from test_area_service import message
from test_direct_publish import PUBLISHER, area_with_direct, denied, service
from test_people_and_files import CALLER, service_with_correlation

G_SEND = "99999999-0000-0000-0000-000000000099"
NAMES = {
    G_SEND: "r-grpAg-TeamsSendMessage",
    G_VAMBE: "r-grpAg-Vambee",
    G_ASISS: "r-grpAg-TeamsTD-MCP",
    G_LIQUID: "r-grpAg-TeamsTD",
}


def run(coro):
    return asyncio.run(coro)


def test_juanito_with_agent_access_but_outside_send_group_learns_why_and_how():
    """Juanito tiene el agente y el grupo del área, pero no el de mensajes directos."""
    svc = service_with_correlation()
    svc.direct_message_group = G_SEND
    svc.groups.names = NAMES
    with pytest.raises(RequestDenied) as exc:
        run(svc.direct_message_scope(None, CALLER, "asistencia"))
    text = exc.value.message
    assert exc.value.status_code == 403
    assert "mensajes directos ni archivos" in text and "«r-grpAg-TeamsSendMessage»" in text
    assert HOW_TO_REQUEST in text


def test_agent_denied_in_teams_names_the_missing_group():
    svc = service_for([])
    svc.groups.names = NAMES
    outcome = run(svc.handle_incoming(message(text="vambe leads")))
    assert "para **Vambe** necesitas pertenecer al grupo de Entra «r-grpAg-Vambee»" in outcome.user_message
    assert HOW_TO_REQUEST in outcome.user_message


def test_direct_publish_denial_names_the_publisher_group():
    svc = service(area_with_direct(), {PUBLISHER: [G_LIQUID]})
    svc.groups.names = NAMES
    exc = denied(svc, "rrhh_reportes")
    assert exc.status_code == 403
    assert "«rrhh_reportes»" in exc.message and "«r-grpAg-TeamsTD-MCP»" in exc.message
    assert HOW_TO_REQUEST in exc.message


def test_unknown_destination_explains_alias_usage():
    svc = service(area_with_direct(), {PUBLISHER: [G_ASISS]})
    exc = denied(svc, "Canal de publicaciones")
    assert exc.status_code == 404 and "alias del canal" in exc.message


def test_unknown_agent_lists_enabled_agents():
    svc = service_with_correlation()
    with pytest.raises(RequestDenied) as exc:
        run(svc.direct_message_scope(None, CALLER, "fantasma"))
    assert exc.value.status_code == 404 and "asistencia" in exc.value.message


def test_lookup_failure_asks_to_retry():
    svc = service(replace(make_area(), direct_publish=DirectPublishRule("any", (G_ASISS,), ("rrhh_reportes",))))
    svc.groups.fail = True
    assert denied(svc, "rrhh_reportes").message == RETRY_LOOKUP


def test_group_names_fall_back_to_ids_when_graph_cannot_name_them():
    svc = service(area_with_direct(), {PUBLISHER: [G_LIQUID]})
    assert G_ASISS in denied(svc, "rrhh_reportes").message


def test_requirement_wording():
    names = {"a": "A", "b": "B", "c": "C"}
    assert describe_requirements([("any", ("a",))], names) == "pertenecer al grupo de Entra «A»"
    assert describe_requirements([("any", ("a", "b"))], names) == (
        "pertenecer a uno de estos grupos de Entra: «A» o «B»"
    )
    assert describe_requirements([("all", ("a", "b", "c"))], names) == (
        "pertenecer a los grupos de Entra «A», «B» y «C»"
    )
    assert describe_requirements([("any", ("a",)), ("any", ("b",))], names) == (
        "pertenecer al grupo de Entra «A» y además pertenecer al grupo de Entra «B»"
    )
    assert unmet_rule("all", ["a", "b"], ["a"]) == ("all", ("b",))
    assert unmet_rule("any", ["a", "b"], ["b"]) is None
