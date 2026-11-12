"""Descubrimiento de canales y edición parcial de áreas."""

import asyncio
from dataclasses import replace

import pytest

from area_service import RequestDenied
from areas import AreaValidationError, DirectPublishRule
from channel_sync import ChannelSync, TeamSnapshot, group_by_team
from conftest import CH_RRHH, CH_RRHH_REPORTES, G_ASISS, G_LIQUID, make_area
from stores import KnownChannel
from test_admin import client, token
from test_area_service import FakeChannels, build_service

TEAM = "19:team-rrhh@thread.tacv2"
CH_NUEVO = "19:nuevo@thread.tacv2"
SERVICE_URL = "https://smba.trafficmanager.net/amer/"
AUTH = {"Authorization": token()}


def run(coro):
    return asyncio.run(coro)


def known(conversation_id, name, team=TEAM, team_name="RRHH"):
    return KnownChannel(conversation_id, name, team, team_name, "", SERVICE_URL, "2026-11-12")


class Fetcher:
    def __init__(self, channels, team_name="RRHH", fail=False):
        self.channels = channels
        self.team_name = team_name
        self.fail = fail
        self.calls = []

    async def __call__(self, team_id, service_url):
        self.calls.append((team_id, service_url))
        if self.fail:
            raise RuntimeError("Teams caído")
        return TeamSnapshot(team_id, self.team_name, "aad-1", tuple(self.channels))


# ---------------- Sincronización ----------------

def test_sync_records_every_channel_of_the_team():
    store = FakeChannels()
    sync = ChannelSync(store, Fetcher([(TEAM, None), (CH_RRHH, "Pagos"), (CH_NUEVO, "Nuevo")]))
    result = run(sync.sync_team(TEAM, SERVICE_URL))
    assert result.recorded == 3 and result.removed == 0
    names = {c.conversation_id: c.channel_name for c in store.recorded}
    assert names == {TEAM: "General", CH_RRHH: "Pagos", CH_NUEVO: "Nuevo"}
    assert all(c.team_name == "RRHH" and c.service_url == SERVICE_URL for c in store.recorded)
    assert all(c.team_aad_group_id == "aad-1" for c in store.recorded)


def test_sync_removes_deleted_channels_of_that_team_only():
    store = FakeChannels()
    store.recorded = [known(CH_RRHH, "Pagos"), known(CH_NUEVO, "Viejo"), known("19:otro", "Otro", team="19:otro-team")]
    sync = ChannelSync(store, Fetcher([(CH_RRHH, "Pagos renombrado")]))
    result = run(sync.sync_team(TEAM, SERVICE_URL))
    ids = {c.conversation_id for c in store.recorded}
    assert result.removed == 1 and CH_NUEVO not in ids and "19:otro" in ids
    assert next(c for c in store.recorded if c.conversation_id == CH_RRHH).channel_name == "Pagos renombrado"


def test_sync_safely_never_raises():
    sync = ChannelSync(FakeChannels(), Fetcher([], fail=True))
    assert run(sync.sync_safely(TEAM, SERVICE_URL)) is None
    assert run(sync.sync_safely("", SERVICE_URL)) is None


def test_sync_known_teams_uses_stored_service_url():
    store = FakeChannels()
    store.recorded = [known(CH_RRHH, "Pagos")]
    fetcher = Fetcher([(CH_RRHH, "Pagos")])
    results = run(ChannelSync(store, fetcher).sync_known_teams())
    assert [r.team_id for r in results] == [TEAM] and fetcher.calls == [(TEAM, SERVICE_URL)]
    with pytest.raises(KeyError):
        run(ChannelSync(store, fetcher).sync_known_teams("19:desconocido"))


def test_group_by_team_marks_assigned_channels():
    teams = group_by_team(
        [known(CH_RRHH, "Pagos"), known(CH_NUEVO, "Nuevo"), known("19:x", "X", team="19:t2", team_name="Otro")],
        {CH_RRHH: ("rrhh", "rrhh_general")},
    )
    assert [t["teamName"] for t in teams] == ["Otro", "RRHH"]
    rrhh = teams[1]["channels"]
    assert {c["channelName"]: (c["areaId"], c["alias"]) for c in rrhh} == {
        "Nuevo": (None, None), "Pagos": ("rrhh", "rrhh_general"),
    }


# ---------------- Edición parcial (servicio) ----------------

def service_with_known(*extra):
    svc = build_service(
        areas=[make_area()], known=(CH_RRHH, CH_RRHH_REPORTES, *extra),
        secrets=("agent-workspace-token",),
    )
    svc.channels.recorded = [known(CH_NUEVO, "Ventas")]
    return svc


def test_set_channel_adds_with_known_name():
    svc = service_with_known(CH_NUEVO)
    area = run(svc.set_channel("rrhh", "RRHH_Ventas", {"conversationId": CH_NUEVO}, "admin"))
    channel = area.channel_by_alias("rrhh_ventas")
    assert channel.conversation_id == CH_NUEVO and channel.display_name == "Ventas"
    assert len(area.channels) == 3 and area.updated_by == "admin"


def test_set_channel_rejects_unknown_conversation():
    svc = service_with_known()
    with pytest.raises(AreaValidationError, match="Canales desconocidos"):
        run(svc.set_channel("rrhh", "rrhh_x", {"conversationId": "19:nadie@thread.tacv2"}, "admin"))


def test_remove_channel_also_cleans_direct_publish():
    svc = service_with_known()
    svc.areas.items["rrhh"] = replace(
        make_area(), direct_publish=DirectPublishRule("any", (G_ASISS,), ("rrhh_reportes",))
    )
    area = run(svc.remove_channel("rrhh", "rrhh_reportes", "admin"))
    assert area.channel_by_alias("rrhh_reportes") is None and area.direct_publish is None
    with pytest.raises(RequestDenied) as exc:
        run(svc.remove_channel("rrhh", "no_existe", "admin"))
    assert exc.value.status_code == 404


def test_set_agent_adds_and_updates_partially():
    svc = service_with_known()
    area = run(svc.set_agent("rrhh", "Liquidaciones", {
        "displayName": "Liquidaciones", "triggerId": "agtch_bbbbbbbbbbbbbbbb",
        "requiredGroups": [G_LIQUID],
    }, "admin"))
    added = area.agent_by_alias("liquidaciones")
    assert added.required_groups == (G_LIQUID,) and len(area.agents) == 2

    area = run(svc.set_agent("rrhh", "liquidaciones", {"description": "Nueva"}, "admin"))
    updated = area.agent_by_alias("liquidaciones")
    assert updated.description == "Nueva" and updated.trigger_id == "agtch_bbbbbbbbbbbbbbbb"
    assert updated.required_groups == (G_LIQUID,)


def test_set_agent_is_validated():
    svc = service_with_known()
    with pytest.raises(AreaValidationError, match="triggerId"):
        run(svc.set_agent("rrhh", "malo", {"displayName": "Malo", "triggerId": "x"}, "admin"))


def test_remove_agent_clears_default_and_keeps_minimum():
    svc = service_with_known()
    run(svc.set_agent("rrhh", "liquidaciones", {
        "displayName": "Liq", "triggerId": "agtch_bbbbbbbbbbbbbbbb"}, "admin"))
    svc.areas.items["rrhh"] = replace(svc.areas.items["rrhh"], default_agent="liquidaciones")
    area = run(svc.remove_agent("rrhh", "liquidaciones", "admin"))
    assert area.agent_by_alias("liquidaciones") is None and area.default_agent == ""
    with pytest.raises(AreaValidationError, match="al menos un agente"):
        run(svc.remove_agent("rrhh", "asistencia", "admin"))


def test_edit_missing_area_is_404():
    with pytest.raises(RequestDenied) as exc:
        run(service_with_known().remove_agent("nada", "x", "admin"))
    assert exc.value.status_code == 404


# ---------------- Endpoints ----------------

def test_edit_endpoints_require_admin_and_work():
    svc = service_with_known(CH_NUEVO)
    c = client(svc)
    url = "/api/admin/areas/rrhh/channels/rrhh_ventas"
    assert c.put(url, json={"conversationId": CH_NUEVO}).status_code == 401
    ok = c.put(url, json={"conversationId": CH_NUEVO}, headers=AUTH)
    assert ok.status_code == 200 and any(ch["alias"] == "rrhh_ventas" for ch in ok.json()["channels"])

    agent = c.put("/api/admin/areas/rrhh/agents/liq", headers=AUTH, json={
        "displayName": "Liq", "triggerId": "agtch_bbbbbbbbbbbbbbbb", "requiredGroups": [G_LIQUID]})
    assert agent.status_code == 200
    bad = c.put("/api/admin/areas/rrhh/agents/liq", headers=AUTH, json={"triggerId": "x"})
    assert bad.status_code == 400
    assert c.put("/api/admin/areas/rrhh/agents/Mal Alias", headers=AUTH, json={}).status_code == 400

    assert c.delete("/api/admin/areas/rrhh/agents/liq", headers=AUTH).status_code == 200
    assert c.delete(url, headers=AUTH).status_code == 200
    assert c.delete(url, headers=AUTH).status_code == 404
    assert c.put(url, headers=AUTH, content=b"[1]").status_code == 400


def test_teams_endpoint_groups_channels():
    svc = service_with_known()
    svc.channels.recorded = [known(CH_RRHH, "Pagos"), known(CH_NUEVO, "Ventas")]
    r = client(svc).get("/api/admin/teams", headers=AUTH)
    assert r.status_code == 200
    channels = r.json()["teams"][0]["channels"]
    assert {c["channelName"]: c["areaId"] for c in channels} == {"Pagos": "rrhh", "Ventas": None}


def test_sync_endpoint():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from admin_api import build_admin_router
    from test_admin import validator

    svc = service_with_known()
    svc.channels.recorded = [known(CH_RRHH, "Pagos")]
    fetcher = Fetcher([(CH_RRHH, "Pagos"), (CH_NUEVO, "Ventas")])
    app = FastAPI()
    app.include_router(build_admin_router(svc, validator(), ChannelSync(svc.channels, fetcher)))
    c = TestClient(app)

    r = c.post("/api/admin/teams/sync", headers=AUTH)
    assert r.status_code == 200 and r.json()["teams"][0]["channels"] == 2
    assert c.post("/api/admin/teams/sync", headers=AUTH, json={"teamId": "19:x"}).status_code == 404
    fetcher.fail = True
    assert c.post("/api/admin/teams/sync", headers=AUTH).status_code == 502
    assert client(svc).post("/api/admin/teams/sync", headers=AUTH).status_code == 503
