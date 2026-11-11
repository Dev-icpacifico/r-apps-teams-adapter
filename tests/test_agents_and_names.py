import asyncio

import httpx
import pytest

import agents as agents_module
from agents import AgentConfigurationError, AgentInvoker, build_agent_input, conversation_key
from channel_names import ChannelNameResolver, TeamNames
from conftest import TRIGGER_A, agent, make_area


def run(coro):
    return asyncio.run(coro)


class FakeSecrets:
    def __init__(self, values):
        self.values = values
        self.read = []

    async def get(self, name):
        self.read.append(name)
        return self.values[name]


# ---------------- Invocación de agentes ----------------

def test_trigger_builds_url_body_and_headers(monkeypatch):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = request.read().decode()
        return httpx.Response(202)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        agents_module.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )
    secrets = FakeSecrets({"agent-workspace-token": "tok-comun"})
    invoker = AgentInvoker(secrets, default_token_secret="agent-workspace-token")
    area = make_area()
    status = run(invoker.trigger(
        area, area.agents[0], "hola", "cid-1", conversation_key="teams-k", idempotency_key="teams-1"
    ))
    assert status == 202
    assert seen["url"] == f"https://api.chatgpt.com/v1/workspace_agents/{TRIGGER_A}/trigger"
    assert seen["headers"]["authorization"] == "Bearer tok-comun"
    assert seen["headers"]["idempotency-key"] == "teams-1"
    assert '"conversation_key":"teams-k"' in seen["body"].replace(" ", "")
    assert "agente: asistencia" in seen["body"]


def test_agent_token_overrides_default(monkeypatch):
    async def fake_post(url, token, agent_input, timeout, **kwargs):
        return token

    monkeypatch.setattr(agents_module, "post_to_agent", fake_post)
    secrets = FakeSecrets({"comun": "A", "propio": "B"})
    invoker = AgentInvoker(secrets, default_token_secret="comun")
    area = make_area()
    assert run(invoker.trigger(area, agent("uny", token_secret="propio"), "x", "c")) == "B"
    assert run(invoker.trigger(area, agent("ads"), "x", "c")) == "A"


def test_missing_token_configuration_fails():
    invoker = AgentInvoker(FakeSecrets({}), default_token_secret="")
    area = make_area()
    with pytest.raises(AgentConfigurationError, match="sin token"):
        run(invoker.trigger(area, area.agents[0], "x", "c"))


def test_agent_input_lists_area_context():
    area = make_area()
    text = build_agent_input("informe", "cid", area, area.agents[0])
    assert "area: rrhh" in text and "agente: asistencia" in text
    assert "destinos_permitidos: rrhh_general, rrhh_reportes" in text


def test_agent_input_tells_agent_how_to_answer_in_teams():
    area = make_area()
    text = build_agent_input("informe", "cid-9", area, area.agents[0], "MCP Teams MultiDEV")
    assert 'enviar_respuesta_teams del complemento «MCP Teams MultiDEV» con correlation_id="cid-9"' in text
    assert "publicar_mensaje_teams" in text and "destinos_permitidos" in text


def test_legacy_agent_input_is_unchanged():
    assert build_agent_input("hola", "cid", None) == "hola\n\ncorrelation_id: cid"


def test_conversation_key_is_stable_and_per_agent():
    k1 = conversation_key("rrhh", "uny", "19:x;messageid=1")
    assert k1 == conversation_key("rrhh", "uny", "19:x;messageid=1")
    assert k1 != conversation_key("rrhh", "ads", "19:x;messageid=1")
    assert k1 != conversation_key("rrhh", "uny", "19:x;messageid=2")
    assert len(k1) <= 64


# ---------------- Nombres de canal ----------------

def test_channel_names_are_cached_and_refreshed_for_new_channel():
    calls = []

    async def fetch(team_id, service_url):
        calls.append(team_id)
        return TeamNames("Transformación Digital", {"19:a": "Mensajes", "19:b": "Publicaciones"})

    resolver = ChannelNameResolver(fetch)
    assert run(resolver.names("19:team", "19:a", "url")) == ("Transformación Digital", "Mensajes")
    assert run(resolver.names("19:team", "19:b", "url")) == ("Transformación Digital", "Publicaciones")
    assert calls == ["19:team"]
    run(resolver.names("19:team", "19:nuevo", "url"))
    assert calls == ["19:team", "19:team"]


def test_general_channel_uses_team_id():
    async def fetch(team_id, service_url):
        return TeamNames("Equipo", {})

    assert run(ChannelNameResolver(fetch).names("19:team", "19:team", "")) == ("Equipo", "General")


def test_channel_name_failure_never_raises():
    async def fetch(team_id, service_url):
        raise RuntimeError("Teams caído")

    assert run(ChannelNameResolver(fetch).names("19:team", "19:a", "")) == ("", "")
