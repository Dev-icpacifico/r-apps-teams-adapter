"""Personas de Teams: destinatarios de mensajes directos y menciones.

Las personas se buscan en el roster (lista de miembros) de los equipos donde
está instalada la app, con la API del bot: no requiere permisos de Graph.
Opcionalmente, si el registro de la app tiene ``User.ReadBasic.All``, se
puede buscar en Entra a quien no esté en ningún equipo con la app (ver
``GraphPeople``).

Menciones: el texto puede incluir ``@correo`` (o ``@<Object ID>``) de las
personas indicadas; se reemplaza por ``<at>Nombre</at>`` con su entidad de
mención. Si una persona indicada no aparece en el texto, la mención se
antepone al mensaje.
"""

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger("teams-adapter.people")

MAX_MENTIONS = 20
GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
EMAIL = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")


class PersonNotFound(Exception):
    """La persona no está en los equipos consultados (o no existe)."""

    def __init__(self, identifiers: list[str]):
        super().__init__(", ".join(identifiers))
        self.identifiers = identifiers


class InvalidIdentifier(ValueError):
    pass


@dataclass(frozen=True)
class TeamMember:
    teams_id: str  # 29:… (id de la persona para el bot)
    aad_object_id: str
    name: str
    email: str = ""
    upn: str = ""
    team_id: str = ""
    service_url: str = ""
    is_guest: bool = False

    def matches(self, identifier: str) -> bool:
        return identifier in {self.aad_object_id.lower(), self.email.lower(), self.upn.lower()} - {""}

    def keys(self) -> set[str]:
        return {self.aad_object_id.lower(), self.email.lower(), self.upn.lower()} - {""}


def normalize_identifier(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("@"):
        text = text[1:]
    if not (GUID.match(text) or EMAIL.match(text)):
        raise InvalidIdentifier(f"identificador de persona inválido: {str(value)[:80]!r} (usar correo o Object ID)")
    return text


def parse_identifiers(value: Any) -> list[str]:
    """Lista o texto separado por comas/espacios/punto y coma."""
    if value in (None, ""):
        return []
    items = value if isinstance(value, list) else re.split(r"[,;\s]+", str(value))
    result = list(dict.fromkeys(normalize_identifier(i) for i in items if str(i).strip()))
    if len(result) > MAX_MENTIONS:
        raise InvalidIdentifier(f"máximo {MAX_MENTIONS} menciones por mensaje")
    return result


FetchMembers = Callable[[str, str], Awaitable[list[TeamMember]]]


class RosterDirectory:
    """Miembros de equipos (con caché) obtenidos con la API del bot."""

    def __init__(self, fetch_members: FetchMembers, cache_seconds: int = 600):
        self._fetch = fetch_members
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, list[TeamMember]]] = {}

    async def members(self, team_id: str, service_url: str) -> list[TeamMember]:
        cached = self._cache.get(team_id)
        if cached and time.monotonic() - cached[0] < self._cache_seconds:
            return cached[1]
        members = await self._fetch(team_id, service_url)
        self._cache[team_id] = (time.monotonic(), members)
        return members

    async def find(self, identifier: str, teams: Iterable[tuple[str, str]]) -> TeamMember | None:
        for team_id, service_url in dict.fromkeys(teams):
            if not team_id:
                continue
            try:
                members = await self.members(team_id, service_url)
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo leer el roster del equipo | equipo=%s", team_id)
                continue
            found = next((m for m in members if m.matches(identifier)), None)
            if found is not None:
                return found
        return None

    async def find_all(self, identifiers: list[str], teams: list[tuple[str, str]]) -> list[TeamMember]:
        found, missing = [], []
        for ident in identifiers:
            member = await self.find(ident, teams)
            if member is None:
                missing.append(ident)
            else:
                found.append(member)
        if missing:
            raise PersonNotFound(missing)
        return found


def apply_mentions(text: str, members: list[TeamMember]) -> str:
    """Reemplaza ``@correo``/``@ObjectID`` por ``<at>Nombre</at>``; las
    personas que no aparecen en el texto se mencionan al inicio."""
    prefix = []
    for m in members:
        tag = f"<at>{m.name}</at>"
        replaced = False
        for key in sorted(m.keys(), key=len, reverse=True):
            pattern = re.compile(r"@" + re.escape(key) + r"\b", re.IGNORECASE)
            text, n = pattern.subn(tag, text)
            replaced = replaced or n > 0
        if not replaced:
            prefix.append(tag)
    return (" ".join(prefix) + " " + text) if prefix else text


def mention_entities(members: list[TeamMember]) -> list[dict[str, Any]]:
    return [
        {
            "type": "mention",
            "text": f"<at>{m.name}</at>",
            "mentioned": {"id": m.teams_id, "name": m.name},
        }
        for m in members
    ]


class GraphPeople:
    """Búsqueda opcional en Entra de personas que no están en ningún equipo
    con la app. Requiere el permiso de aplicación ``User.ReadBasic.All``;
    sin él, Graph responde 403 y la búsqueda devuelve None."""

    def __init__(self, credential, http_client=None):
        import httpx

        self._credential = credential
        self._http = http_client or httpx.AsyncClient(timeout=15)

    async def _token(self) -> str:
        token = await self._credential.get_token("https://graph.microsoft.com/.default")
        return token.token

    async def find(self, identifier: str) -> dict[str, str] | None:
        url = f"https://graph.microsoft.com/v1.0/users/{identifier}?$select=id,displayName,mail,userPrincipalName,userType"
        response = await self._http.get(url, headers={"Authorization": f"Bearer {await self._token()}"})
        if response.status_code in (403, 404):
            if response.status_code == 403:
                logger.warning("Graph sin permiso para leer usuarios (falta User.ReadBasic.All)")
            return None
        response.raise_for_status()
        data = response.json()
        return {
            "id": str(data.get("id", "")).lower(),
            "name": str(data.get("displayName", "")),
            "email": str(data.get("mail") or ""),
            "upn": str(data.get("userPrincipalName") or ""),
            "is_guest": str(data.get("userType") or "").lower() == "guest",
        }

    async def install_app_for_user(self, user_id: str, catalog_app_id: str) -> str | None:
        """Instala la app (publicada en el catálogo de la organización) para
        la persona y devuelve el id del chat personal con el bot. Requiere
        ``TeamsAppInstallation.ReadWriteSelfForUser.All``."""
        headers = {"Authorization": f"Bearer {await self._token()}"}
        base = f"https://graph.microsoft.com/v1.0/users/{user_id}/teamwork/installedApps"
        response = await self._http.post(base, headers=headers, json={
            "teamsApp@odata.bind": f"https://graph.microsoft.com/v1.0/appCatalogs/teamsApps/{catalog_app_id}"
        })
        if response.status_code not in (201, 409):
            logger.warning("No se pudo instalar la app para la persona | status=%s", response.status_code)
            return None
        listing = await self._http.get(
            f"{base}?$expand=teamsAppDefinition&$filter=teamsApp/id eq '{catalog_app_id}'", headers=headers
        )
        if listing.status_code != 200 or not listing.json().get("value"):
            return None
        install_id = listing.json()["value"][0]["id"]
        chat = await self._http.get(f"{base}/{install_id}/chat", headers=headers)
        if chat.status_code != 200:
            return None
        return str(chat.json().get("id", "")) or None


# Palabras que pedían mencionar el canal: Teams no lo admite desde un bot y se ignoran.
CHANNEL_MENTION_WORDS = {"canal", "channel"}


_BULLET = re.compile(r"^\s*[•·◦▪‣]\s*")
_LIST = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")


def teams_text(text: str) -> str:
    """Ajusta el texto al Markdown de Teams, donde un salto de línea simple
    se muestra como espacio y «•» no es viñeta: convierte «•» en listas y
    separa las líneas normales en párrafos."""
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    expanded: list[str] = []
    for line in lines:
        # Viñetas escritas en una misma línea: «Título • a • b».
        parts = [p.strip() for p in line.split(" • ")] if line.count(" • ") >= 2 else [line]
        if len(parts) > 1:
            if parts[0] and not _BULLET.match(parts[0]):
                expanded.append(parts[0])
                parts = parts[1:]
            expanded += ["- " + _BULLET.sub("", p) for p in parts if p]
            continue
        expanded.append("- " + _BULLET.sub("", line) if _BULLET.match(line) else line)
    out: list[str] = []
    for line in expanded:
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        is_item = bool(_LIST.match(line))
        if out and out[-1] != "":
            prev_item = bool(_LIST.match(out[-1]))
            if not (is_item and prev_item):
                out.append("")  # párrafo nuevo (o inicio/fin de lista)
        out.append(line.rstrip())
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
