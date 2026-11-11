"""Consulta de pertenencia a grupos de Entra ID mediante Microsoft Graph.

Se consulta desde el lado del grupo, con pertenencia transitiva:
``GET /groups/{id}/transitiveMembers/microsoft.graph.user?$filter=id eq '{user}'``.
Basta el permiso de aplicación GroupMember.Read.All (con consentimiento de
administrador). ``/users/{id}/checkMemberGroups`` exigiría además
User.ReadBasic.All, por eso no se usa.
"""

import asyncio
import logging
import re
import time

import httpx

logger = logging.getLogger("teams-adapter.graph")

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
MAX_CONCURRENT_CALLS = 5
_GUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class GroupLookupError(RuntimeError):
    """No fue posible determinar la pertenencia: se debe denegar."""


class DenyAllGroups:
    """Usado cuando Graph no está configurado: siempre deniega."""

    async def member_groups(self, aad_object_id: str, group_ids: list[str]) -> set[str]:
        raise GroupLookupError("Graph no configurado")


class GraphGroupChecker:
    def __init__(self, credential, cache_seconds: int = 300, http_client: httpx.AsyncClient | None = None):
        self._credential = credential
        self._cache_seconds = cache_seconds
        self._cache: dict[tuple[str, frozenset[str]], tuple[float, frozenset[str]]] = {}
        self._http = http_client

    async def member_groups(self, aad_object_id: str, group_ids: list[str]) -> set[str]:
        """Devuelve el subconjunto de group_ids al que pertenece el usuario."""
        if not aad_object_id or not _GUID.match(aad_object_id):
            raise GroupLookupError("usuario sin aad_object_id válido")
        if not group_ids:
            return set()

        user_id = aad_object_id.lower()
        key = (user_id, frozenset(g.lower() for g in group_ids))
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self._cache_seconds:
            return set(cached[1])

        token = await self._credential.get_token(GRAPH_SCOPE)
        headers = {
            "Authorization": f"Bearer {token.token}",
            # $count y $filter sobre miembros requieren consulta avanzada.
            "ConsistencyLevel": "eventual",
        }
        ids = sorted(key[1])
        limit = asyncio.Semaphore(MAX_CONCURRENT_CALLS)

        client = self._http or httpx.AsyncClient(timeout=15.0)
        try:
            async def is_member(group_id: str) -> bool:
                if not _GUID.match(group_id):
                    raise GroupLookupError("grupo inválido")
                async with limit:
                    response = await client.get(
                        f"{GRAPH_URL}/groups/{group_id}/transitiveMembers/microsoft.graph.user",
                        params={
                            "$filter": f"id eq '{user_id}'",
                            "$count": "true",
                            "$select": "id",
                        },
                        headers=headers,
                    )
                if response.status_code == 404:
                    # Grupo inexistente o eliminado: no otorga pertenencia.
                    logger.warning("Grupo requerido no encontrado en Entra | grupo=%s", group_id)
                    return False
                if response.status_code != 200:
                    logger.error(
                        "Graph transitiveMembers respondió HTTP %s", response.status_code
                    )
                    raise GroupLookupError(f"Graph HTTP {response.status_code}")
                members = response.json().get("value", [])
                return any(str(m.get("id", "")).lower() == user_id for m in members)

            results = await asyncio.gather(*(is_member(g) for g in ids))
        except httpx.HTTPError as exc:
            raise GroupLookupError(type(exc).__name__) from exc
        finally:
            if self._http is None:
                await client.aclose()

        matched = frozenset(g for g, ok in zip(ids, results) if ok)
        self._cache[key] = (time.monotonic(), matched)
        return set(matched)
