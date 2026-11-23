import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from area_service import (
    MSG_CHANNEL_NOT_ENABLED,
    MSG_ERROR,
    MSG_FORBIDDEN,
    MSG_NOT_CHANNEL,
    AreaService,
    IncomingMessage,
    RequestDenied,
)
from areas import AreaValidationError
from conftest import CH_CONT, CH_RRHH, CH_SIN_AREA, G_BOD, G_FIN, G_LIQUID, TRIGGER_B, agent, make_area
from graph_groups import GroupLookupError
from stores import Correlation


def run(coro):
    return asyncio.run(coro)


class FakeAreas:
    def __init__(self, areas):
        self.items = {a.id: a for a in areas}

    async def list_areas(self, use_cache=True):
        return list(self.items.values())

    async def get_area(self, area_id):
        return self.items.get(area_id)

    async def save_area(self, area):
        self.items[area.id] = area


class FakeChannels:
    def __init__(self, known=()):
        self.known = set(known)
        self.recorded = []

    async def record(self, channel):
        # Igual que la tabla real: upsert por conversation_id.
        self.recorded = [c for c in self.recorded if c.conversation_id != channel.conversation_id]
        self.recorded.append(channel)
        self.known.add(channel.conversation_id)

    async def exists(self, conversation_id):
        return conversation_id in self.known

    async def list_channels(self):
        return list(self.recorded)

    async def get(self, conversation_id):
        return next((c for c in self.recorded if c.conversation_id == conversation_id), None)

    async def delete(self, conversation_id):
        self.recorded = [c for c in self.recorded if c.conversation_id != conversation_id]
        self.known.discard(conversation_id)


class FakeCorrelations:
    def __init__(self):
        self.items = {}
        self.history = []  # (conversation_id, area_id, agent_alias)

    async def last_agent(self, conversation_id, area_id):
        for conv, area, alias in reversed(self.history):
            if conv == conversation_id and area == area_id and alias:
                return alias
        return ""

    async def save(self, correlation_id, conversation, area_id="", channel_conversation_id="", agent_alias=""):
        self.history.append((conversation["conversation_id"], area_id, agent_alias))
        self.items[correlation_id] = Correlation(
            correlation_id=correlation_id,
            conversation_id=conversation["conversation_id"],
            area_id=area_id,
            channel_conversation_id=channel_conversation_id,
            aad_object_id=conversation["aad_object_id"],
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            responded_at="",
        )

    async def get(self, correlation_id):
        return self.items.get(correlation_id)

    def put(self, correlation):
        self.items[correlation.correlation_id] = correlation


class FakeGroups:
    def __init__(self, membership=None, fail=False):
        self.membership = membership or {}
        self.fail = fail
        self.calls = 0

    async def member_groups(self, aad_object_id, group_ids):
        self.calls += 1
        if self.fail:
            raise GroupLookupError("Graph caído")
        return {g for g in self.membership.get(aad_object_id, ()) if g in group_ids}

    async def group_names(self, group_ids):
        names = getattr(self, "names", {})
        return {g: names[g] for g in group_ids if g in names}


class FakeAgents:
    def __init__(self, status=202, error=None, default_token_secret="agent-workspace-token"):
        self.status = status
        self.error = error
        self.calls = []
        self.kwargs = []
        self.default_token_secret = default_token_secret

    def token_secret_for(self, agent):
        return agent.token_secret or self.default_token_secret

    async def trigger(self, area, agent, mensaje, correlation_id, **kwargs):
        self.calls.append((area.id, mensaje, correlation_id, agent.alias))
        self.kwargs.append(kwargs)
        if self.error:
            raise self.error
        return self.status


class FakeCatalog:
    """Catálogo global de agentes en memoria."""

    def __init__(self, agents=None):
        from areas import AreaAgent
        from conftest import TRIGGER_B, agent as make_agent

        if agents is None:
            agents = [
                make_agent(),
                AreaAgent("liquidaciones", "Liquidaciones", TRIGGER_B, "", "token-liq"),
            ]
        self.items = {a.alias: a for a in agents}
        self.saved = []

    async def all(self, use_cache=True):
        return dict(self.items)

    async def get(self, alias):
        return self.items.get(alias)

    async def save(self, agent, updated_at, updated_by):
        self.items[agent.alias] = agent
        self.saved.append((agent.alias, updated_by))

    async def delete(self, alias):
        self.items.pop(alias, None)


class FakeSecrets:
    def __init__(self, names=()):
        self.names = set(names)

    async def exists(self, name):
        return name in self.names


def build_service(areas=None, membership=None, groups=None, agents=None, known=(), secrets=(), catalog=None):
    return AreaService(
        areas=FakeAreas(areas if areas is not None else [make_area(), make_area(
            "contabilidad", mode="all", groups=(G_FIN, G_BOD), channels=(("contabilidad", CH_CONT),)
        )]),
        channels=FakeChannels(known),
        correlations=FakeCorrelations(),
        groups=groups or FakeGroups(membership or {}),
        agents=agents or FakeAgents(),
        secrets=FakeSecrets(secrets),
        catalog=catalog if catalog is not None else FakeCatalog(),
    )


def message(channel=CH_RRHH, user="user-1", text="informe de asistencia", activity_id="123"):
    return IncomingMessage(
        text=text,
        conversation_id=f"{channel};messageid=123" if channel else "a:chat-personal",
        channel_conversation_id=channel,
        channel_name="General",
        team_id="team-1",
        team_name="RRHH",
        team_aad_group_id="",
        service_url="https://smba.trafficmanager.net/amer/",
        tenant_id="tenant",
        user_id="29:abc",
        aad_object_id=user,
        bot_id="28:bot",
        channel_id="msteams",
        activity_id=activity_id,
        bot_name="Agentes ICP",
    )


# ---------------- Entrada desde Teams ----------------

def test_user_with_one_group_reaches_area_agent():
    agents = FakeAgents()
    service = build_service(membership={"user-1": [G_LIQUID]}, agents=agents)
    outcome = run(service.handle_incoming(message()))
    assert outcome.status == "accepted" and "Asistencia" in outcome.user_message
    assert outcome.area_id == "rrhh"
    assert agents.calls[0][0] == "rrhh"
    saved = service.correlations.items[outcome.correlation_id]
    assert saved.area_id == "rrhh" and saved.channel_conversation_id == CH_RRHH


def test_user_without_groups_is_rejected_and_agent_not_called():
    agents = FakeAgents()
    service = build_service(membership={"user-1": []}, agents=agents)
    outcome = run(service.handle_incoming(message()))
    assert outcome.status == "forbidden" and "necesitas" in outcome.user_message
    assert "solicitar" in outcome.user_message
    assert agents.calls == [] and service.correlations.items == {}


def test_all_mode_requires_both_groups():
    partial = build_service(membership={"user-1": [G_FIN]})
    assert run(partial.handle_incoming(message(CH_CONT))).status == "forbidden"
    full = build_service(membership={"user-1": [G_FIN, G_BOD]})
    assert run(full.handle_incoming(message(CH_CONT))).status == "accepted"


def test_graph_failure_denies():
    service = build_service(groups=FakeGroups(fail=True))
    assert run(service.handle_incoming(message())).status == "forbidden"


def test_channel_without_area_is_not_enabled_but_recorded():
    service = build_service(membership={"user-1": [G_LIQUID]})
    outcome = run(service.handle_incoming(message(CH_SIN_AREA)))
    assert outcome.user_message == MSG_CHANNEL_NOT_ENABLED
    assert service.channels.recorded[0].conversation_id == CH_SIN_AREA


def test_disabled_area_is_not_enabled():
    service = build_service(areas=[make_area(enabled=False)], membership={"user-1": [G_LIQUID]})
    assert run(service.handle_incoming(message())).status == "channel_not_enabled"


def test_personal_chat_is_rejected_without_checking_groups():
    groups = FakeGroups({"user-1": [G_LIQUID]})
    service = build_service(groups=groups)
    outcome = run(service.handle_incoming(message(channel=None)))
    assert outcome.user_message == MSG_NOT_CHANNEL and groups.calls == 0


def test_agent_error_returns_generic_message():
    service = build_service(membership={"user-1": [G_LIQUID]}, agents=FakeAgents(error=RuntimeError("x")))
    assert run(service.handle_incoming(message())).user_message == MSG_ERROR


def test_agent_non_202_returns_error():
    service = build_service(membership={"user-1": [G_LIQUID]}, agents=FakeAgents(status=401))
    assert run(service.handle_incoming(message())).status == "error"


# ---------------- Varios agentes por área ----------------

def multi_area(default_agent=""):
    return make_area(
        agents=(agent("asistencia"), agent("liquidaciones", TRIGGER_B, "token-liq")),
        default_agent=default_agent,
    )


def test_alias_routes_to_that_agent_and_strips_alias():
    agents = FakeAgents()
    service = build_service(areas=[multi_area()], membership={"user-1": [G_LIQUID]}, agents=agents)
    outcome = run(service.handle_incoming(message(text="liquidaciones: revisa marzo")))
    assert outcome.status == "accepted" and outcome.agent_alias == "liquidaciones"
    assert agents.calls[0][1] == "revisa marzo" and agents.calls[0][3] == "liquidaciones"
    assert agents.kwargs[0]["idempotency_key"] == "teams-123"
    assert agents.kwargs[0]["conversation_key"].startswith("teams-")


def test_alias_is_case_insensitive_and_handles_html_spaces():
    agents = FakeAgents()
    service = build_service(areas=[multi_area()], membership={"user-1": [G_LIQUID]}, agents=agents)
    outcome = run(service.handle_incoming(message(text="&nbsp;Asistencia\xa0informe de hoy")))
    assert outcome.agent_alias == "asistencia" and agents.calls[0][1] == "informe de hoy"


@pytest.mark.parametrize("text", ["agentes", "AYUDA", "", "help"])
def test_list_command_shows_area_agents_without_triggering(text):
    agents = FakeAgents()
    service = build_service(areas=[multi_area()], membership={"user-1": [G_LIQUID]}, agents=agents)
    outcome = run(service.handle_incoming(message(text=text)))
    assert outcome.status == "agents_list"
    assert "**asistencia**" in outcome.user_message and "**liquidaciones**" in outcome.user_message
    assert "@Agentes ICP" in outcome.user_message
    assert agents.calls == [] and service.correlations.items == {}


def test_list_command_requires_area_permissions():
    service = build_service(areas=[multi_area()], membership={"user-1": []})
    assert run(service.handle_incoming(message(text="agentes"))).status == "forbidden"


def test_without_alias_and_several_agents_asks_to_choose():
    agents = FakeAgents()
    service = build_service(areas=[multi_area()], membership={"user-1": [G_LIQUID]}, agents=agents)
    outcome = run(service.handle_incoming(message(text="hazme un informe")))
    assert outcome.status == "choose_agent" and agents.calls == []


def test_without_alias_uses_default_agent():
    agents = FakeAgents()
    service = build_service(
        areas=[multi_area(default_agent="liquidaciones")], membership={"user-1": [G_LIQUID]}, agents=agents
    )
    outcome = run(service.handle_incoming(message(text="hazme un informe")))
    assert outcome.agent_alias == "liquidaciones" and agents.calls[0][1] == "hazme un informe"


def test_single_agent_receives_full_text():
    agents = FakeAgents()
    service = build_service(membership={"user-1": [G_LIQUID]}, agents=agents)
    run(service.handle_incoming(message(text="hazme un informe")))
    assert agents.calls[0][1] == "hazme un informe"


def test_alias_without_text_asks_for_request():
    agents = FakeAgents()
    service = build_service(areas=[multi_area()], membership={"user-1": [G_LIQUID]}, agents=agents)
    outcome = run(service.handle_incoming(message(text="liquidaciones")))
    assert outcome.status == "missing_text" and agents.calls == []


def test_alias_of_other_area_is_not_used():
    agents = FakeAgents()
    other = make_area("contabilidad", channels=(("contabilidad", CH_CONT),), agents=(agent("balance", TRIGGER_B),))
    service = build_service(areas=[multi_area(), other], membership={"user-1": [G_LIQUID]}, agents=agents)
    outcome = run(service.handle_incoming(message(text="balance del mes")))
    assert outcome.status == "choose_agent" and agents.calls == []


# ---------------- Publicación ----------------

def accepted_correlation(service, channel=CH_RRHH):
    outcome = run(service.handle_incoming(message(channel)))
    assert outcome.status == "accepted"
    return outcome.correlation_id


def test_publish_in_same_area_is_allowed():
    service = build_service(membership={"user-1": [G_LIQUID]})
    cid = accepted_correlation(service)
    area, channel = run(service.resolve_publish("rrhh_reportes", cid))
    assert area.id == "rrhh" and channel.alias == "rrhh_reportes"


def test_publish_to_other_area_is_denied():
    service = build_service(membership={"user-1": [G_LIQUID]})
    cid = accepted_correlation(service)
    with pytest.raises(RequestDenied) as exc:
        run(service.resolve_publish("contabilidad", cid))
    assert exc.value.status_code == 403


def test_publish_unknown_alias_is_404():
    service = build_service()
    with pytest.raises(RequestDenied) as exc:
        run(service.resolve_publish("no_existe", None))
    assert exc.value.status_code == 404


def test_publish_without_request_requires_area_flag():
    service = build_service()
    with pytest.raises(RequestDenied, match="requiere responder a una solicitud"):
        run(service.resolve_publish("rrhh_general", None))
    open_service = build_service(areas=[make_area(allow_without_request=True)])
    area, _ = run(open_service.resolve_publish("rrhh_general", None))
    assert area.id == "rrhh"


def test_publish_with_unknown_or_expired_correlation_is_denied():
    service = build_service()
    with pytest.raises(RequestDenied):
        run(service.resolve_publish("rrhh_general", "inventado"))
    service.correlations.put(Correlation(
        "vieja", "conv", "rrhh", CH_RRHH, "user-1",
        datetime.now(timezone.utc) - timedelta(minutes=1), "",
    ))
    with pytest.raises(RequestDenied):
        run(service.resolve_publish("rrhh_general", "vieja"))


def test_publish_to_disabled_area_is_denied():
    service = build_service(areas=[make_area(enabled=False, allow_without_request=True)])
    with pytest.raises(RequestDenied, match="deshabilitada"):
        run(service.resolve_publish("rrhh_general", None))


# ---------------- Callback ----------------

def test_callback_rules():
    service = build_service(membership={"user-1": [G_LIQUID]})
    cid = accepted_correlation(service)
    assert run(service.authorize_callback(cid)).area_id == "rrhh"

    with pytest.raises(RequestDenied) as missing:
        run(service.authorize_callback("no-existe"))
    assert missing.value.status_code == 404

    service.correlations.put(Correlation("resp", "c", "rrhh", CH_RRHH, "u", None, "2026-11-10T00:00:00+00:00"))
    with pytest.raises(RequestDenied) as responded:
        run(service.authorize_callback("resp"))
    assert responded.value.status_code == 409


# ---------------- Administración ----------------

def payload(channel=CH_RRHH, alias="rrhh_general"):
    return {
        "displayName": "RRHH",
        "accessMode": "any",
        "requiredGroups": [G_LIQUID],
        "channels": [{"alias": alias, "conversationId": channel}],
        "agents": ["asistencia", "liquidaciones"],
    }


def test_upsert_requires_known_channel_and_catalog_agents():
    service = build_service(areas=[], known=())
    with pytest.raises(AreaValidationError, match="Canales desconocidos"):
        run(service.upsert_area("rrhh", payload(), "admin"))

    service = build_service(areas=[], known=(CH_RRHH,))
    with pytest.raises(AreaValidationError, match="no existen en el catálogo.*otro"):
        run(service.upsert_area("rrhh", {**payload(), "agents": ["asistencia", "otro"]}, "admin"))

    area = run(service.upsert_area("rrhh", payload(), "admin@icp"))
    assert service.areas.items["rrhh"].agent_refs == ("asistencia", "liquidaciones")
    assert area.updated_by == "admin@icp"
    assert [a.alias for a in area.agents] == ["asistencia", "liquidaciones"]
    assert area.agents[1].token_secret == "token-liq"


def test_upsert_rejects_channel_of_other_area():
    service = build_service(known=(CH_CONT,), secrets=("agent-workspace-token", "token-liq"))
    with pytest.raises(AreaValidationError, match="ya pertenece"):
        run(service.upsert_area("nueva", payload(CH_CONT, "nuevo_alias"), "admin"))


def test_disable_and_enable_area():
    service = build_service()
    assert run(service.set_enabled("rrhh", False, "admin")).enabled is False
    assert run(service.set_enabled("rrhh", True, "admin")).enabled is True
    with pytest.raises(RequestDenied):
        run(service.set_enabled("no-existe", False, "admin"))
