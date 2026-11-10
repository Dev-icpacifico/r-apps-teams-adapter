import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from areas import Area, AreaChannel  # noqa: E402

G_LIQUID = "11111111-1111-1111-1111-111111111111"
G_ASISS = "22222222-2222-2222-2222-222222222222"
G_GENERAL = "33333333-3333-3333-3333-333333333333"
G_FIN = "44444444-4444-4444-4444-444444444444"
G_BOD = "55555555-5555-5555-5555-555555555555"

CH_RRHH = "19:rrhh-general@thread.tacv2"
CH_RRHH_REPORTES = "19:rrhh-reportes@thread.tacv2"
CH_CONT = "19:contabilidad@thread.tacv2"
CH_SIN_AREA = "19:sin-area@thread.tacv2"


def make_area(
    area_id="rrhh",
    mode="any",
    groups=(G_LIQUID, G_ASISS, G_GENERAL),
    channels=(("rrhh_general", CH_RRHH), ("rrhh_reportes", CH_RRHH_REPORTES)),
    enabled=True,
    allow_without_request=False,
):
    return Area(
        id=area_id,
        display_name=area_id.upper(),
        access_mode=mode,
        required_groups=tuple(groups),
        channels=tuple(AreaChannel(a, c, a) for a, c in channels),
        agent_trigger_url_secret=f"agent-{area_id}-trigger-url",
        agent_token_secret=f"agent-{area_id}-token",
        enabled=enabled,
        allow_publish_without_request=allow_without_request,
    )


@pytest.fixture
def area_rrhh():
    return make_area()


@pytest.fixture
def area_contabilidad():
    return make_area(
        "contabilidad",
        mode="all",
        groups=(G_FIN, G_BOD),
        channels=(("contabilidad", CH_CONT),),
    )
