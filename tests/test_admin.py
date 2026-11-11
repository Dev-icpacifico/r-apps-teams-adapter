import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin_api import build_admin_router
from admin_auth import AdminAuthError, EntraAdminValidator
from conftest import CH_RRHH, G_LIQUID
from test_area_service import build_service

TENANT = "61a303d1-5e6a-4536-a416-10b77c682985"
AUDIENCE = "api://teams-adapter-dev"
ROLE = "teams.areas.admin"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeSigningKey:
    def __init__(self, key):
        self.key = key


class FakeJwks:
    def get_signing_key_from_jwt(self, token):
        return FakeSigningKey(PRIVATE_KEY.public_key())


def token(roles=(ROLE,), aud=AUDIENCE, tid=TENANT, key=PRIVATE_KEY, exp_offset=600, iss=None):
    claims = {
        "aud": aud,
        "iss": iss or f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": tid,
        "oid": "admin-oid",
        "preferred_username": "admin@icpacifico.onmicrosoft.com",
        "roles": list(roles),
        "exp": int(time.time()) + exp_offset,
    }
    return "Bearer " + jwt.encode(claims, key, algorithm="RS256")


def validator(audiences=(AUDIENCE,)):
    return EntraAdminValidator(TENANT, list(audiences), ROLE, jwks_client=FakeJwks())


# ---------------- Validación de tokens ----------------

def test_valid_admin_token():
    principal = validator().validate(token())
    assert principal.actor == "admin@icpacifico.onmicrosoft.com"


def test_v1_issuer_is_accepted():
    validator().validate(token(iss=f"https://sts.windows.net/{TENANT}/"))


@pytest.mark.parametrize(
    "kwargs, status",
    [
        ({"roles": ()}, 403),
        ({"roles": ("teams.areas.reader",)}, 403),
        ({"aud": "api://otra-api"}, 401),
        ({"key": OTHER_KEY}, 401),
        ({"exp_offset": -60}, 401),
        ({"iss": "https://login.microsoftonline.com/otro-tenant/v2.0"}, 401),
    ],
)
def test_invalid_tokens(kwargs, status):
    with pytest.raises(AdminAuthError) as exc:
        validator().validate(token(**kwargs))
    assert exc.value.status_code == status


def test_missing_token_and_unconfigured():
    with pytest.raises(AdminAuthError) as missing:
        validator().validate(None)
    assert missing.value.status_code == 401
    with pytest.raises(AdminAuthError) as unconfigured:
        validator(audiences=()).validate(token())
    assert unconfigured.value.status_code == 503


# ---------------- Endpoints ----------------

def client(service=None):
    app = FastAPI()
    app.include_router(build_admin_router(service or build_service(), validator()))
    return TestClient(app)


def test_list_areas_requires_admin_role():
    c = client()
    assert c.get("/api/admin/areas").status_code == 401
    assert c.get("/api/admin/areas", headers={"Authorization": token(roles=())}).status_code == 403
    response = c.get("/api/admin/areas", headers={"Authorization": token()})
    assert response.status_code == 200
    assert [a["id"] for a in response.json()["areas"]] == ["contabilidad", "rrhh"]


def test_put_area_creates_and_validates():
    service = build_service(areas=[], known=(CH_RRHH,), secrets=("agent-workspace-token",))
    c = client(service)
    body = {
        "displayName": "RRHH",
        "accessMode": "any",
        "requiredGroups": [G_LIQUID],
        "channels": [{"alias": "rrhh_general", "conversationId": CH_RRHH}],
        "agents": [{"alias": "asistencia", "displayName": "Asistencia", "triggerId": "agtch_aaaaaaaaaaaaaaaa"}],
    }
    ok = c.put("/api/admin/areas/rrhh", json=body, headers={"Authorization": token()})
    assert ok.status_code == 200 and ok.json()["updatedBy"].startswith("admin@")
    assert ok.json()["agents"][0]["triggerId"] == "agtch_aaaaaaaaaaaaaaaa"

    bad = c.put("/api/admin/areas/rrhh", json={**body, "accessMode": "x"}, headers={"Authorization": token()})
    assert bad.status_code == 400 and "accessMode" in bad.json()["error"]

    bad_id = c.put("/api/admin/areas/RRHH!", json=body, headers={"Authorization": token()})
    assert bad_id.status_code in (400, 404)


def test_disable_area_endpoint():
    c = client()
    r = c.post("/api/admin/areas/rrhh/disable", headers={"Authorization": token()})
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert c.post("/api/admin/areas/nada/disable", headers={"Authorization": token()}).status_code == 404


def test_get_missing_area_is_404():
    assert client().get("/api/admin/areas/nada", headers={"Authorization": token()}).status_code == 404


def test_storage_failure_returns_503():
    from azure.core.exceptions import HttpResponseError

    service = build_service()

    async def broken(use_cache=True):
        raise HttpResponseError(message="AuthorizationPermissionMismatch")

    service.areas.list_areas = broken
    r = client(service).get("/api/admin/areas", headers={"Authorization": token()})
    assert r.status_code == 503 and "no disponible" in r.json()["error"]
