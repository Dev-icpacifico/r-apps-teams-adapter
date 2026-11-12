"""Publicación directa desde ChatGPT (sin solicitud de Teams)."""

import asyncio

import pytest

from area_service import RequestDenied
from areas import (
    AreaValidationError,
    DirectPublishRule,
    area_from_entity,
    area_to_entity,
    parse_area_payload,
)
from conftest import CH_CONT, CH_RRHH, CH_RRHH_REPORTES, G_ASISS, G_BOD, G_FIN, G_LIQUID, make_area
from dataclasses import replace
from test_area_service import FakeGroups, build_service
from test_policy_and_areas import valid_payload

PUBLISHER = "abcdefab-1234-1234-1234-abcdefabcdef"


def run(coro):
    return asyncio.run(coro)


def area_with_direct(mode="any", groups=(G_ASISS,), channels=("rrhh_reportes",), enabled=True):
    return replace(
        make_area(enabled=enabled),
        direct_publish=DirectPublishRule(mode, tuple(groups), tuple(channels)),
    )


def service(area, membership=None, groups=None):
    return build_service(
        areas=[area, make_area("contabilidad", groups=(G_FIN,), channels=(("contabilidad", CH_CONT),))],
        membership=membership,
        groups=groups,
    )


def denied(svc, alias, oid=PUBLISHER):
    with pytest.raises(RequestDenied) as exc:
        run(svc.resolve_direct_publish(alias, oid))
    return exc.value


# ---------------- Autorización ----------------

def test_publisher_in_group_can_publish_to_allowed_channel():
    svc = service(area_with_direct(), {PUBLISHER: [G_ASISS]})
    area, channel = run(svc.resolve_direct_publish("RRHH_Reportes", PUBLISHER.upper()))
    assert area.id == "rrhh" and channel.conversation_id == CH_RRHH_REPORTES


def test_publisher_outside_group_is_denied():
    svc = service(area_with_direct(), {PUBLISHER: [G_LIQUID]})
    assert denied(svc, "rrhh_reportes").status_code == 403


def test_channel_not_listed_is_denied_even_for_publisher():
    svc = service(area_with_direct(), {PUBLISHER: [G_ASISS]})
    exc = denied(svc, "rrhh_general")
    assert exc.status_code == 403 and "canal" in exc.message


def test_area_without_rule_rejects_direct_publish():
    svc = service(make_area(), {PUBLISHER: [G_ASISS, G_LIQUID]})
    exc = denied(svc, "rrhh_reportes")
    assert exc.status_code == 403 and "no permite" in exc.message


def test_area_publish_without_request_flag_does_not_enable_direct_publish():
    area = replace(make_area(allow_without_request=True))
    svc = service(area, {PUBLISHER: [G_ASISS]})
    assert denied(svc, "rrhh_reportes").status_code == 403


def test_missing_or_invalid_caller_is_rejected_before_lookup():
    groups = FakeGroups({PUBLISHER: [G_ASISS]})
    svc = service(area_with_direct(), groups=groups)
    for oid in ("", "no-es-guid", None):
        assert denied(svc, "rrhh_reportes", oid).status_code == 401
    assert groups.calls == 0


def test_unknown_destination_and_disabled_area():
    svc = service(area_with_direct(), {PUBLISHER: [G_ASISS]})
    assert denied(svc, "no_existe").status_code == 404
    off = service(area_with_direct(enabled=False), {PUBLISHER: [G_ASISS]})
    assert denied(off, "rrhh_reportes").status_code == 403


def test_all_mode_requires_every_group():
    area = area_with_direct(mode="all", groups=(G_FIN, G_BOD))
    assert denied(service(area, {PUBLISHER: [G_FIN]}), "rrhh_reportes").status_code == 403
    ok = service(area, {PUBLISHER: [G_FIN, G_BOD]})
    assert run(ok.resolve_direct_publish("rrhh_reportes", PUBLISHER))[1].alias == "rrhh_reportes"


def test_group_lookup_failure_denies():
    svc = service(area_with_direct(), groups=FakeGroups(fail=True))
    assert denied(svc, "rrhh_reportes").status_code == 403


def test_other_area_channel_is_not_covered_by_this_rule():
    svc = service(area_with_direct(), {PUBLISHER: [G_ASISS, G_FIN]})
    assert denied(svc, "contabilidad").status_code == 403


# ---------------- Payload y persistencia ----------------

def direct(**overrides):
    rule = {"accessMode": "any", "requiredGroups": [G_ASISS], "channels": ["rrhh_general"]}
    rule.update(overrides)
    return rule


def test_payload_parses_direct_publish():
    area = parse_area_payload("rrhh", valid_payload(directPublish=direct(channels=["RRHH_General"])))
    assert area.direct_publish == DirectPublishRule("any", (G_ASISS,), ("rrhh_general",))
    assert area.to_public_dict()["directPublish"]["channels"] == ["rrhh_general"]


def test_payload_without_direct_publish_is_none():
    area = parse_area_payload("rrhh", valid_payload())
    assert area.direct_publish is None and area.to_public_dict()["directPublish"] is None


@pytest.mark.parametrize(
    "rule, expected",
    [
        (direct(requiredGroups=[]), "directPublish.requiredGroups"),
        (direct(channels=[]), "directPublish.channels"),
        (direct(channels=["otro_canal"]), "no es un canal del área"),
        (direct(accessMode="some"), "accessMode"),
        ("si", "directPublish debe ser un objeto"),
    ],
)
def test_invalid_direct_publish(rule, expected):
    with pytest.raises(AreaValidationError, match=expected):
        parse_area_payload("rrhh", valid_payload(directPublish=rule))


def test_entity_roundtrip_keeps_direct_publish():
    area = area_with_direct(mode="all", groups=(G_FIN, G_BOD), channels=("rrhh_general", "rrhh_reportes"))
    restored = area_from_entity(area_to_entity(area))
    assert restored.direct_publish == area.direct_publish


def test_entity_without_direct_publish_field_loads():
    entity = area_to_entity(make_area())
    entity.pop("direct_publish")
    assert area_from_entity(entity).direct_publish is None
