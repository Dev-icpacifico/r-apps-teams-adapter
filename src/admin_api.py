"""Endpoint de administración de áreas.

Pensado para la aplicación interna que gestiona grupos de Entra ID:
lista canales donde está el bot, crea o modifica áreas y las habilita o
deshabilita. No borra áreas para conservar el historial.
"""

import logging

from azure.core.exceptions import AzureError
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from admin_auth import AdminAuthError, AdminPrincipal, EntraAdminValidator
from area_service import AreaService, RequestDenied
from areas import AREA_ID_PATTERN, AreaValidationError

MAX_BODY_BYTES = 64 * 1024

logger = logging.getLogger("teams-adapter.admin")


class StorageSafeRoute(APIRoute):
    """Responde 503 cuando el almacenamiento o Key Vault no están disponibles."""

    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await original(request)
            except AzureError as exc:
                logger.error("Almacenamiento no disponible en administración | %s", type(exc).__name__)
                return JSONResponse(
                    status_code=503,
                    content={"error": "Almacenamiento de áreas no disponible"},
                )

        return handler


def build_admin_router(service: AreaService, validator: EntraAdminValidator) -> APIRouter:
    router = APIRouter(prefix="/api/admin", route_class=StorageSafeRoute)

    async def principal(request: Request) -> AdminPrincipal | JSONResponse:
        try:
            return await validator.validate_async(request.headers.get("authorization"))
        except AdminAuthError as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    def bad_area_id(area_id: str) -> JSONResponse | None:
        if not AREA_ID_PATTERN.match(area_id):
            return JSONResponse(status_code=400, content={"error": "id de área inválido"})
        return None

    @router.get("/areas")
    async def list_areas(request: Request):
        who = await principal(request)
        if isinstance(who, JSONResponse):
            return who
        areas = await service.areas.list_areas(use_cache=False)
        return {"areas": [a.to_public_dict() for a in sorted(areas, key=lambda a: a.id)]}

    @router.get("/areas/{area_id}")
    async def get_area(area_id: str, request: Request):
        who = await principal(request)
        if isinstance(who, JSONResponse):
            return who
        if (error := bad_area_id(area_id)) is not None:
            return error
        area = await service.areas.get_area(area_id)
        if area is None:
            return JSONResponse(status_code=404, content={"error": "Área no encontrada"})
        return area.to_public_dict()

    @router.put("/areas/{area_id}")
    async def put_area(area_id: str, request: Request):
        who = await principal(request)
        if isinstance(who, JSONResponse):
            return who
        if (error := bad_area_id(area_id)) is not None:
            return error
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"error": "Cuerpo demasiado grande"})
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(status_code=400, content={"error": "JSON inválido"})
        try:
            area = await service.upsert_area(area_id, payload, who.actor)
        except AreaValidationError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return area.to_public_dict()

    @router.post("/areas/{area_id}/disable")
    async def disable_area(area_id: str, request: Request):
        return await _toggle(area_id, request, False)

    @router.post("/areas/{area_id}/enable")
    async def enable_area(area_id: str, request: Request):
        return await _toggle(area_id, request, True)

    async def _toggle(area_id: str, request: Request, enabled: bool):
        who = await principal(request)
        if isinstance(who, JSONResponse):
            return who
        if (error := bad_area_id(area_id)) is not None:
            return error
        try:
            area = await service.set_enabled(area_id, enabled, who.actor)
        except RequestDenied as exc:
            return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
        return area.to_public_dict()

    @router.get("/channels")
    async def list_channels(request: Request):
        who = await principal(request)
        if isinstance(who, JSONResponse):
            return who
        channels = await service.channels.list_channels()
        areas = await service.areas.list_areas(use_cache=False)
        owner = {
            c.conversation_id: a.id for a in areas for c in a.channels
        }
        return {
            "channels": [
                {**c.to_public_dict(), "areaId": owner.get(c.conversation_id)}
                for c in sorted(channels, key=lambda c: (c.team_name, c.channel_name))
            ]
        }

    return router
