"""Invocación de agentes de espacio de trabajo por área.

Cada agente del área se identifica por el ID de su canal API
(``agtch_…``); la URL se arma con ``AGENT_TRIGGER_BASE_URL``. El token
(Workspace Agent access token) se lee desde Key Vault: el del agente si
tiene uno propio, o el común configurado en ``AGENT_DEFAULT_TOKEN_SECRET``.
"""

import hashlib
import logging
import time

import httpx

from areas import Area, AreaAgent

logger = logging.getLogger("teams-adapter.agents")

DEFAULT_TRIGGER_BASE_URL = "https://api.chatgpt.com/v1/workspace_agents"


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


def build_agent_input(
    mensaje: str,
    correlation_id: str,
    area: Area | None,
    agent: AreaAgent | None = None,
) -> str:
    """Texto enviado al agente. Incluye el contexto del área para que el
    agente sepa a qué destinos puede publicar."""
    parts = [mensaje, "", f"correlation_id: {correlation_id}"]
    if area is not None:
        aliases = ", ".join(c.alias for c in area.channels)
        parts.append(f"area: {area.id}")
        if agent is not None:
            parts.append(f"agente: {agent.alias}")
        parts.append(f"destinos_permitidos: {aliases}")
    return "\n".join(parts)


def conversation_key(area_id: str, agent_alias: str, conversation_id: str) -> str:
    """Clave estable por hilo de Teams y agente: permite que el agente
    continúe la misma conversación cuando le responden en el hilo."""
    digest = hashlib.sha256(
        f"{area_id}|{agent_alias}|{conversation_id}".encode("utf-8")
    ).hexdigest()
    return f"teams-{digest[:48]}"


class AgentInvoker:
    def __init__(
        self,
        secrets: SecretResolver | None,
        default_token_secret: str = "",
        base_url: str = DEFAULT_TRIGGER_BASE_URL,
        timeout: float = 30.0,
    ):
        self._secrets = secrets
        self.default_token_secret = default_token_secret
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def token_secret_for(self, agent: AreaAgent) -> str:
        return agent.token_secret or self.default_token_secret

    def trigger_url(self, agent: AreaAgent) -> str:
        return f"{self._base_url}/{agent.trigger_id}/trigger"

    async def trigger(
        self,
        area: Area,
        agent: AreaAgent,
        mensaje: str,
        correlation_id: str,
        conversation_key: str = "",
        idempotency_key: str = "",
    ) -> int:
        if self._secrets is None:
            raise AgentConfigurationError("KEY_VAULT_URI no configurada")
        token_secret = self.token_secret_for(agent)
        if not token_secret:
            raise AgentConfigurationError(
                f"Agente {agent.alias!r} sin token: definir tokenSecret o "
                "AGENT_DEFAULT_TOKEN_SECRET"
            )
        token = await self._secrets.get(token_secret)
        return await post_to_agent(
            self.trigger_url(agent),
            token,
            build_agent_input(mensaje, correlation_id, area, agent),
            self._timeout,
            conversation_key=conversation_key,
            idempotency_key=idempotency_key,
        )


async def post_to_agent(
    url: str,
    token: str,
    agent_input: str,
    timeout: float = 30.0,
    conversation_key: str = "",
    idempotency_key: str = "",
) -> int:
    body: dict[str, str] = {"input": agent_input}
    if conversation_key:
        body["conversation_key"] = conversation_key
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=headers)
    return response.status_code
