"""Continuidad en hilos de Teams: varias personas, mismo agente y contexto."""

import asyncio
from dataclasses import replace

from agents import build_agent_input, conversation_key
from area_service import route_message
from areas import AreaAgent
from conftest import CH_RRHH, G_LIQUID, TRIGGER_A, TRIGGER_B, make_area
from test_area_service import FakeAgents, build_service, message

G_UNY = "88888888-8888-8888-8888-888888888888"
THREAD = f"{CH_RRHH};messageid=1700"


def run(coro):
    return asyncio.run(coro)


def area():
    return make_area(
        groups=(G_LIQUID,),
        agents=[
            AreaAgent("unyagent", "UnyAgent", TRIGGER_A, required_groups=(G_UNY,)),
            AreaAgent("noticias", "Noticias", TRIGGER_B),
        ],
    )


def in_thread(user, text, activity_id):
    return replace(message(user=user, text=text, activity_id=activity_id), conversation_id=THREAD, user_name=user)


def test_follow_up_without_alias_continues_with_thread_agent():
    agents = FakeAgents()
    svc = build_service(
        areas=[area()], agents=agents,
        membership={"vicente": [G_LIQUID, G_UNY], "otra": [G_LIQUID, G_UNY]},
    )
    first = run(svc.handle_incoming(in_thread("vicente", "unyagent último pedido de compra", "1")))
    assert first.status == "accepted" and first.agent_alias == "unyagent"

    follow = run(svc.handle_incoming(in_thread("otra", "¿y la semana anterior?", "2")))
    assert follow.status == "accepted" and follow.agent_alias == "unyagent"
    assert agents.calls[1][1] == "¿y la semana anterior?"
    # Misma conversación del agente para ambas personas del hilo.
    assert agents.kwargs[0]["conversation_key"] == agents.kwargs[1]["conversation_key"]
    assert agents.kwargs[1]["requested_by"] == "otra"


def test_thread_agent_still_requires_permission():
    agents = FakeAgents()
    svc = build_service(
        areas=[area()], agents=agents,
        membership={"vicente": [G_LIQUID, G_UNY], "luis": [G_LIQUID]},
    )
    run(svc.handle_incoming(in_thread("vicente", "unyagent ventas", "1")))
    denied = run(svc.handle_incoming(in_thread("luis", "¿y en octubre?", "2")))
    assert denied.status == "agent_forbidden" and denied.agent_alias == "unyagent"
    assert len(agents.calls) == 1


def test_explicit_alias_overrides_thread_agent():
    svc = build_service(areas=[area()], membership={"vicente": [G_LIQUID, G_UNY]})
    run(svc.handle_incoming(in_thread("vicente", "unyagent ventas", "1")))
    switched = run(svc.handle_incoming(in_thread("vicente", "noticias resumen de hoy", "2")))
    assert switched.agent_alias == "noticias"
    again = run(svc.handle_incoming(in_thread("vicente", "¿y ayer?", "3")))
    assert again.agent_alias == "noticias"


def test_other_threads_do_not_inherit_agent():
    svc = build_service(areas=[area()], membership={"vicente": [G_LIQUID, G_UNY]})
    run(svc.handle_incoming(in_thread("vicente", "unyagent ventas", "1")))
    other = replace(message(user="vicente", text="¿y ayer?", activity_id="9"), conversation_id=f"{CH_RRHH};messageid=9999")
    assert run(svc.handle_incoming(other)).status == "choose_agent"


def test_route_message_thread_agent_only_without_alias():
    a = area()
    assert route_message(a, "¿y ayer?", list(a.agents), "unyagent").agent.alias == "unyagent"
    assert route_message(a, "noticias hoy", list(a.agents), "unyagent").agent.alias == "noticias"
    assert route_message(a, "agentes", list(a.agents), "unyagent").kind == "list"
    assert route_message(a, "¿y ayer?", list(a.agents), "ya-no-existe").kind == "choose"


def test_agent_input_names_requester_and_shared_thread():
    a = area()
    text = build_agent_input("¿y ayer?", "cid-1", a, a.agents[0], "MCP Teams MultiDEV", "Luis Pizarro")
    assert "solicitado_por: Luis Pizarro" in text and "varias personas" in text
    assert "solicitado_por" not in build_agent_input("hola", "cid-2", a, a.agents[0])


def test_conversation_key_is_per_thread_and_agent_not_per_person():
    assert conversation_key("fin", "unyagent", THREAD) == conversation_key("fin", "unyagent", THREAD)
    assert conversation_key("fin", "unyagent", THREAD) != conversation_key("fin", "noticias", THREAD)
