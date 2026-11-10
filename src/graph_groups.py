"""Consulta de pertenencia a grupos de Entra ID mediante Microsoft Graph.

Usa checkMemberGroups, que incluye pertenencia transitiva. Requiere el
permiso de aplicación GroupMember.Read.All (con consentimiento de
administrador) en el registro de aplicación usado por el adaptador.
"""

import logging
import time

import httpx

logger = logging.getLogger("teams-adapter.graph")

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_URL = "https://graph.microsoft.com/v1.0"
MAX_GROUPS_PER_CALL = 20  # límite de checkMemberGroups


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
        if not aad_object_id:
            raise GroupLookupError("usuario sin aad_object_id")
        if not group_ids:
            return set()

        key = (aad_object_id.lower(), frozenset(g.lower() for g in group_ids))
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < self._cache_seconds:
            return set(cached[1])

        token = await self._credential.get_token(GRAPH_SCOPE)
        headers = {"Authorization": f"Bearer {token.token}"}
        matched: set[str] = set()
        ids = sorted(key[1])

        client = self._http or httpx.AsyncClient(timeout=15.0)
        try:
            for start in range(0, len(ids), MAX_GROUPS_PER_CALL):
                chunk = ids[start:start + MAX_GROUPS_PER_CALL]
                response = await client.post(
                    f"{GRAPH_URL}/users/{aad_object_id}/checkMemberGroups",
                    json={"groupIds": chunk},
                    headers=headers,
                )
                if response.status_code != 200:
                    logger.error(
                        "Graph checkMemberGroups respondió HTTP %s", response.status_code
                    )
                    raise GroupLookupError(f"Graph HTTP {response.status_code}")
                matched.update(g.lower() for g in response.json().get("value", []))
        except httpx.HTTPError as exc:
            raise GroupLookupError(type(exc).__name__) from exc
        finally:
            if self._http is None:
                await client.aclose()

        self._cache[key] = (time.monotonic(), frozenset(matched))
        return matched
