"""Configuración del adaptador leída desde variables de entorno."""

import os
from dataclasses import dataclass, field


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "si", "sí"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value and value.strip() else default
    except ValueError:
        return default


def _list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    # Modo multiagente por áreas. Desactivado = comportamiento heredado
    # (un solo agente y tabla TeamsDestinations).
    areas_enabled: bool = False

    storage_account_name: str = "stteamsadapterprod"
    correlations_table: str = "TeamsCorrelations"
    destinations_table: str = "TeamsDestinations"
    areas_table: str = "TeamsAreas"
    channels_table: str = "TeamsKnownChannels"

    key_vault_uri: str | None = None
    correlation_ttl_hours: int = 24
    areas_cache_seconds: int = 60
    group_cache_seconds: int = 300

    tenant_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None

    admin_audiences: list[str] = field(default_factory=list)
    admin_required_role: str = "teams.areas.admin"

    # Agentes por área: token común (nombre del secreto en Key Vault) y
    # URL base del disparo; cada agente aporta su ID agtch_….
    agent_default_token_secret: str = ""
    agent_trigger_base_url: str = "https://api.chatgpt.com/v1/workspace_agents"
    channel_names_cache_seconds: int = 600
    # Nombre visible del complemento MCP de Teams que deben usar los
    # agentes para responder (p. ej. «MCP Teams MultiDEV» en DEV).
    agent_teams_plugin_name: str = ""
    # Nombre visible del bot si un mensaje llega sin mención (p. ej. «Agentes ICP DEV»).
    teams_bot_display_name: str = ""

    # Modo heredado
    agent_trigger_url: str | None = None
    agent_access_token: str | None = None

    teams_callback_key: str | None = None
    teams_publish_key: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.getenv
        return cls(
            areas_enabled=_bool(env("TEAMS_AREAS_ENABLED")),
            storage_account_name=env("STORAGE_ACCOUNT_NAME", "stteamsadapterprod"),
            correlations_table=env("TEAMS_CORRELATIONS_TABLE", "TeamsCorrelations"),
            destinations_table=env("TEAMS_DESTINATIONS_TABLE", "TeamsDestinations"),
            areas_table=env("TEAMS_AREAS_TABLE", "TeamsAreas"),
            channels_table=env("TEAMS_CHANNELS_TABLE", "TeamsKnownChannels"),
            key_vault_uri=env("KEY_VAULT_URI") or None,
            correlation_ttl_hours=_int(env("CORRELATION_TTL_HOURS"), 24),
            areas_cache_seconds=_int(env("AREAS_CACHE_SECONDS"), 60),
            group_cache_seconds=_int(env("GROUP_CACHE_SECONDS"), 300),
            tenant_id=env("TENANT_ID") or None,
            client_id=env("CLIENT_ID") or None,
            client_secret=env("CLIENT_SECRET") or None,
            admin_audiences=_list(env("ADMIN_API_AUDIENCE")),
            admin_required_role=env("ADMIN_REQUIRED_ROLE", "teams.areas.admin"),
            agent_default_token_secret=env("AGENT_DEFAULT_TOKEN_SECRET", "") or "",
            agent_trigger_base_url=env(
                "AGENT_TRIGGER_BASE_URL", "https://api.chatgpt.com/v1/workspace_agents"
            ),
            channel_names_cache_seconds=_int(env("CHANNEL_NAMES_CACHE_SECONDS"), 600),
            agent_teams_plugin_name=env("AGENT_TEAMS_PLUGIN_NAME", "") or "",
            teams_bot_display_name=env("TEAMS_BOT_DISPLAY_NAME", "") or "",
            agent_trigger_url=env("AGENT_TRIGGER_URL") or None,
            agent_access_token=env("AGENT_ACCESS_TOKEN") or None,
            teams_callback_key=env("TEAMS_CALLBACK_KEY") or None,
            teams_publish_key=env("TEAMS_PUBLISH_KEY") or None,
        )
