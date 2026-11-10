"""Comprueba que la aplicación carga en ambos modos sin llamar a Azure."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

SCRIPT = """
import main
routes = sorted(main.fastapi_app.openapi()["paths"])
print("AREAS", main.area_service is not None)
print("ROUTES", ",".join(routes))
"""


def load(env_overrides):
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("TEAMS_", "AGENT_", "KEY_VAULT", "ADMIN_", "CLIENT_", "TENANT_"))
    }
    env.update({
        "STORAGE_ACCOUNT_NAME": "stfakelocal",
        "TEAMS_CALLBACK_KEY": "x",
        "TEAMS_PUBLISH_KEY": "y",
        **env_overrides,
    })
    result = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=SRC, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_legacy_mode_has_no_admin_routes():
    out = load({})
    assert "AREAS False" in out
    assert "/api/teams-publish" in out and "/api/admin/areas" not in out


@pytest.mark.parametrize("with_graph", [True, False])
def test_areas_mode_exposes_admin_routes(with_graph):
    env = {
        "TEAMS_AREAS_ENABLED": "true",
        "KEY_VAULT_URI": "https://kv-fake.vault.azure.net/",
        "ADMIN_API_AUDIENCE": "api://teams-adapter-dev",
        "TENANT_ID": "61a303d1-5e6a-4536-a416-10b77c682985",
    }
    if with_graph:
        env.update({"CLIENT_ID": "00000000-0000-0000-0000-000000000001", "CLIENT_SECRET": "fake"})
    out = load(env)
    assert "AREAS True" in out
    assert "/api/admin/areas" in out and "/api/admin/channels" in out
