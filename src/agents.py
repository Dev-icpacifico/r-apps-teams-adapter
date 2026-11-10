"""Invocación de agentes de espacio de trabajo por área.

La URL de disparo y el token de cada agente se leen desde Key Vault
usando los nombres de secreto registrados en el área.
"""

import logging
import time

import httpx

from areas import Area

logger = logging.getLogger("teams-adapter.agents")


class AgentConfigurationError(RuntimeError):
    pass


class SecretResolver:
    """Lee secretos de Key Vault con caché corta."""

    def __init__(self, secret_client, cache_seconds: int = 600):
        self._client = secret_client
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, str]] = {}

    async def get(self, name: str) -> str:
        cached = self._cache.get(name)
        if cached and time.monotonic() - cached[0] < self._cache_seconds:
            return cached[1]
        if self._client is None:
            raise AgentConfigurationError("KEY_VAULT_URI no configurada")
        secret = await self._client.get_secret(name)
        if not secret.value:
            raise AgentConfigurationError(f"secreto vacío: {name}")
        self._cache[name] = (time.monotonic(), secret.value)
        return secret.value

    async def exists(self, name: str) -> bool:
        try:
            await self.get(name)
            return True
        except Exception:  # noqa: BLE001 - cualquier fallo = no utilizable
            return False


def build_agent_input(mensaje: str, correlation_id: str, area: Area | None) -> str:
    """Texto enviado al agente. Incluye el contexto del área para que el
    agente sepa a qué destinos puede publicar."""
    parts = [mensaje, "", f"correlation_id: {correlation_id}"]
    if area is not None:
        aliases = ", ".join(c.alias for c in area.channels)
        parts.append(f"area: {area.id}")
        parts.append(f"destinos_permitidos: {aliases}")
    return "\n".join(parts)


class AgentInvoker:
    def __init__(self, secrets: SecretResolver | None, timeout: float = 30.0):
        self._secrets = secrets
        self._timeout = timeout

    async def trigger(self, area: Area, mensaje: str, correlation_id: str) -> int:
        if self._secrets is None:
            raise AgentConfigurationError("KEY_VAULT_URI no configurada")
        url = await self._secrets.get(area.agent_trigger_url_secret)
        token = await self._secrets.get(area.agent_token_secret)
        return await post_to_agent(
            url, token, build_agent_input(mensaje, correlation_id, area), self._timeout
        )


async def post_to_agent(url: str, token: str, agent_input: str, timeout: float = 30.0) -> int:
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            url,
            json={"input": agent_input},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
    return response.status_code
