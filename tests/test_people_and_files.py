"""Menciones, mensajes directos y archivos por chat personal."""

import asyncio
import base64
from datetime import datetime, timedelta, timezone

import pytest
from azure.core.exceptions import ResourceNotFoundError

from area_service import RequestDenied
from conftest import CH_RRHH, CH_RRHH_REPORTES, G_LIQUID
from file_transfer import FileValidationError, PendingFileStore, consent_accepted, make_pending, validate_file
from stores import Correlation, KnownChannel
from teams_people import (
    InvalidIdentifier,
    PersonNotFound,
    RosterDirectory,
    TeamMember,
    apply_mentions,
    mention_entities,
    normalize_identifier,
    parse_identifiers,
)
from test_area_service import build_service

LUIS = TeamMember("29:luis", "aaaaaaaa-0000-0000-0000-000000000001", "Luis Pizarro", "lpizarro@icp.cl", "lpizarro@icp.cl", "team-1", "https://smba/")
ANA = TeamMember("29:ana", "aaaaaaaa-0000-0000-0000-000000000002", "Ana Díaz", "adiaz@icp.cl", "adiaz@icp.onmicrosoft.com", "team-1", "https://smba/")
CALLER = "cccccccc-0000-0000-0000-000000000009"


def run(coro):
    return asyncio.run(coro)


# ---------------- Identificadores y menciones ----------------

def test_identifiers_accept_email_or_object_id_only():
    assert normalize_identifier(" @LPizarro@ICP.cl ") == "lpizarro@icp.cl"
    assert parse_identifiers("a@icp.cl; b@icp.cl, a@icp.cl") == ["a@icp.cl", "b@icp.cl"]
    assert parse_identifiers(["AAAAAAAA-0000-0000-0000-000000000001"]) == [LUIS.aad_object_id]
    assert parse_identifiers(None) == [] and parse_identifiers("") == []
    for bad in ("Luis Pizarro", "29:luis", "https://x"):
        with pytest.raises(InvalidIdentifier):
            normalize_identifier(bad)
    with pytest.raises(InvalidIdentifier, match="máximo"):
        parse_identifiers([f"p{i}@icp.cl" for i in range(21)])


def test_mentions_replace_tokens_or_prefix_missing_ones():
    text = apply_mentions("Hola @LPIZARRO@icp.cl, revisa el informe.", [LUIS, ANA])
    assert text == "<at>Ana Díaz</at> Hola <at>Luis Pizarro</at>, revisa el informe."
    by_oid = apply_mentions(f"Ver @{ANA.aad_object_id} y @adiaz@icp.onmicrosoft.com", [ANA])
    assert by_oid == "Ver <at>Ana Díaz</at> y <at>Ana Díaz</at>"
    entity = mention_entities([LUIS])[0]
    assert entity["mentioned"] == {"id": "29:luis", "name": "Luis Pizarro"} and entity["text"] == "<at>Luis Pizarro</at>"


def test_roster_finds_people_and_caches_members():
    calls = []

    async def fetch(team_id, service_url):
        calls.append(team_id)
        return [LUIS, ANA] if team_id == "team-1" else []

    roster = RosterDirectory(fetch)
    teams = [("team-2", "u"), ("team-1", "u")]
    assert run(roster.find("adiaz@icp.onmicrosoft.com", teams)) == ANA
    assert run(roster.find(LUIS.aad_object_id, teams)) == LUIS
    assert calls == ["team-2", "team-1"]  # segunda búsqueda desde caché
    with pytest.raises(PersonNotFound) as missing:
        run(roster.find_all(["lpizarro@icp.cl", "nadie@icp.cl"], teams))
    assert missing.value.identifiers == ["nadie@icp.cl"]


def test_roster_skips_teams_that_fail():
    async def fetch(team_id, service_url):
        if team_id == "roto":
            raise RuntimeError("403")
        return [LUIS]

    assert run(RosterDirectory(fetch).find("lpizarro@icp.cl", [("roto", ""), ("team-1", "")])) == LUIS


# ---------------- Autorización de mensajes directos ----------------
# Regla del usuario (19/11): cualquier persona de la empresa puede recibir;
# quien origina debe tener acceso al agente.

def service_with_correlation(agent_alias="asistencia", expired=False, area_id="rrhh"):
    svc = build_service(membership={ANA.aad_object_id: [G_LIQUID], CALLER: [G_LIQUID]})
    for cid, name in ((CH_RRHH, "General"), (CH_RRHH_REPORTES, "Reportes"), ("19:otro", "Otro")):
        run(svc.channels.record(KnownChannel(cid, name, "team-1" if cid != "19:otro" else "team-2", "RRHH", "", "https://smba/", "x", "standard")))
    svc.correlations.put(Correlation(
        correlation_id="corr-1", conversation_id=f"{CH_RRHH};messageid=1", area_id=area_id,
        channel_conversation_id=CH_RRHH, aad_object_id=LUIS.aad_object_id.upper(),
        expires_at=datetime.now(timezone.utc) + (timedelta(hours=-1) if expired else timedelta(hours=1)),
        responded_at="", agent_alias=agent_alias, tenant_id="tenant",
    ))
    return svc


def test_correlation_scope_uses_its_agent_and_searches_every_team():
    svc = service_with_correlation()
    scope = run(svc.direct_message_scope("corr-1", ""))
    assert scope.origin == "teams" and scope.area.id == "rrhh" and scope.agent.alias == "asistencia"
    assert scope.requester_oid == LUIS.aad_object_id and scope.tenant_id == "tenant"
    assert {t for t, _ in scope.teams} == {"team-1", "team-2"}


def test_correlation_scope_rejects_expired_or_missing():
    with pytest.raises(RequestDenied):
        run(service_with_correlation(expired=True).direct_message_scope("corr-1", ""))
    with pytest.raises(RequestDenied):
        run(service_with_correlation().direct_message_scope("no-existe", ""))


def test_chatgpt_scope_requires_caller_with_access_to_the_agent():
    svc = service_with_correlation()
    scope = run(svc.direct_message_scope(None, CALLER.upper(), "Asistencia"))
    assert scope.origin == "chatgpt" and scope.agent.alias == "asistencia" and scope.area is None
    assert scope.requester_oid == CALLER
    with pytest.raises(RequestDenied) as no_access:
        run(svc.direct_message_scope(None, "dddddddd-0000-0000-0000-000000000003", "asistencia"))
    assert no_access.value.status_code == 403
    with pytest.raises(RequestDenied) as unknown_agent:
        run(svc.direct_message_scope(None, CALLER, "fantasma"))
    assert unknown_agent.value.status_code == 404
    with pytest.raises(RequestDenied) as no_alias:
        run(svc.direct_message_scope(None, CALLER, ""))
    assert no_alias.value.status_code == 400
    with pytest.raises(RequestDenied) as no_caller:
        run(svc.direct_message_scope(None, "no-es-oid", "asistencia"))
    assert no_caller.value.status_code == 401


def test_team_of_conversation_strips_thread_suffix():
    svc = service_with_correlation()
    assert run(svc.team_of_conversation(f"{CH_RRHH};messageid=99")) == [("team-1", "https://smba/")]
    assert run(svc.team_of_conversation("19:desconocido")) == []


# ---------------- Archivos ----------------

class FakeTable:
    def __init__(self):
        self.rows = {}

    async def upsert_entity(self, entity):
        self.rows[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    async def get_entity(self, partition_key, row_key):
        try:
            return dict(self.rows[(partition_key, row_key)])
        except KeyError:
            raise ResourceNotFoundError("no") from None

    async def delete_entity(self, partition_key, row_key):
        self.rows.pop((partition_key, row_key), None)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_validate_file():
    assert validate_file("informe.pdf", b64(b"%PDF")) == ("informe.pdf", b"%PDF")
    assert validate_file("datos.csv", "data:text/csv;base64," + b64(b"a,b")) == ("datos.csv", b"a,b")
    for name, content, msg in (
        ("sin_extension", b64(b"x"), "nombre"),
        ("../x.pdf", b64(b"x"), "nombre"),
        ("x.pdf", "no base64!", "base64"),
        ("x.pdf", "", "contenido"),
        ("x.pdf", b64(b"x" * 11), "supera"),
    ):
        with pytest.raises(FileValidationError, match=msg):
            validate_file(name, content, max_bytes=10)


def test_pending_file_roundtrip_in_parts_and_delete():
    table = FakeTable()
    store = PendingFileStore(table)
    data = bytes(range(256)) * 5000  # 1,28 MB → varias entidades
    pending = make_pending("grande.bin", data, LUIS.aad_object_id.upper(), "Informe", "agente")
    run(store.save(pending))
    assert len(table.rows) > 2
    loaded = run(store.get(pending.id))
    assert loaded.data == data and loaded.recipient_oid == LUIS.aad_object_id and loaded.name == "grande.bin"
    run(store.delete(pending.id))
    assert table.rows == {} and run(store.get(pending.id)) is None
    assert run(store.get("../otro")) is None


def test_pending_file_expires():
    table = FakeTable()
    store = PendingFileStore(table)
    pending = make_pending("a.txt", b"hola", LUIS.aad_object_id, "", "")
    pending.created_at = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    run(store.save(pending))
    assert run(store.get(pending.id)) is None and table.rows == {}


# ---------------- PDF ----------------

def test_pdf_is_rendered_from_markdown_with_tables_and_spanish_text():
    from pdf_render import PdfContentError, render_pdf, safe_filename

    md = "# Noticias\n\n- **Economía:** ¿subió? ¡sí! 🚀\n\n| Comuna | N |\n|---|---|\n| La Serena | 4 |\n"
    pdf = render_pdf("Noticias 19-11", md, "Noticias Diarias")
    assert pdf.startswith(b"%PDF") and len(pdf) > 1000
    assert safe_filename("Noticias del día: 19/11") == "Noticias_del_día_1911.pdf"
    for title, content in (("", "x"), ("t", ""), ("t", "x" * 200_001)):
        with pytest.raises(PdfContentError):
            render_pdf(title, content, "agente")


def test_consent_accept_is_read_from_the_sdk_enum():
    from microsoft_teams.api.models.file.file_consent_card import FileConsentCardResponse

    accept = FileConsentCardResponse.model_validate({"action": "accept", "context": {"fileId": "x"}})
    decline = FileConsentCardResponse.model_validate({"action": "decline", "context": {"fileId": "x"}})
    assert str(accept.action) != "accept"  # el error del 19/11: str() da "Action.ACCEPT"
    assert consent_accepted(accept.action) and not consent_accepted(decline.action)
    assert consent_accepted("accept") and not consent_accepted(None)
