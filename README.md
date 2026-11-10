# Teams Agent Adapter

Backend que conecta Microsoft Teams con agentes de espacio de trabajo. Recibe mensajes del bot (`/api/messages`), invoca al agente y entrega sus respuestas (`/api/teams-callback`) y publicaciones (`/api/teams-publish`) que llegan desde el MCP de Teams.

## Modos de operación

| Modo | Activación | Comportamiento |
|---|---|---|
| Heredado | `TEAMS_AREAS_ENABLED` vacío o `false` | Un solo agente (`AGENT_TRIGGER_URL`, `AGENT_ACCESS_TOKEN`) y destinos de la tabla `TeamsDestinations`. Es el comportamiento original. |
| Multiagente por áreas | `TEAMS_AREAS_ENABLED=true` | Cada canal pertenece a un área; el área define los grupos de Entra ID requeridos, el modo (`any`/`all`) y el agente que atiende. |

### Flujo por áreas

1. Un usuario escribe al bot en un canal. El canal queda registrado en `TeamsKnownChannels`.
2. Se busca el área del canal en `TeamsAreas`. Si no hay área habilitada, se responde que el canal no está habilitado.
3. Se verifica en Microsoft Graph (`checkMemberGroups`, pertenencia transitiva) si el usuario cumple los grupos del área: `any` = al menos uno, `all` = todos. Ante un error se deniega.
4. Se crea una correlación con el área de origen y se invoca al agente del área. La URL y el token del agente se leen desde Key Vault.
5. El agente responde con `enviar_respuesta_teams` (una respuesta por solicitud) y puede publicar con `publicar_mensaje_teams` **sólo en canales de la misma área**, enviando el `correlation_id`.
6. Publicar sin `correlation_id` sólo se permite si el área tiene `allowPublishWithoutRequest=true`.

Chats personales y de grupo no se atienden en este modo.

## Variables de entorno

Comunes: `STORAGE_ACCOUNT_NAME`, `TEAMS_CORRELATIONS_TABLE`, `TEAMS_CALLBACK_KEY`, `TEAMS_PUBLISH_KEY`, `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID`.

Heredado: `AGENT_TRIGGER_URL`, `AGENT_ACCESS_TOKEN`, `TEAMS_DESTINATIONS_TABLE`.

Áreas:

| Variable | Uso | Por defecto |
|---|---|---|
| `TEAMS_AREAS_ENABLED` | Activa el modo por áreas | `false` |
| `TEAMS_AREAS_TABLE` | Catálogo de áreas | `TeamsAreas` |
| `TEAMS_CHANNELS_TABLE` | Canales donde el bot recibió mensajes | `TeamsKnownChannels` |
| `KEY_VAULT_URI` | Key Vault con URL y token de cada agente | — |
| `CORRELATION_TTL_HOURS` | Vigencia de una solicitud para publicar | `24` |
| `AREAS_CACHE_SECONDS` / `GROUP_CACHE_SECONDS` | Caché del catálogo y de grupos | `60` / `300` |
| `ADMIN_API_AUDIENCE` | Audiencia(s) aceptadas por la API de administración, separadas por coma | — |
| `ADMIN_REQUIRED_ROLE` | App Role exigido para administrar | `teams.areas.admin` |

Permisos de Azure/Entra:

- Identidad administrada de la App Service: *Storage Table Data Contributor* en la cuenta de almacenamiento y *Key Vault Secrets User* en el Key Vault.
- Registro de aplicación del bot (`CLIENT_ID`): permiso de aplicación de Microsoft Graph `GroupMember.Read.All` con consentimiento de administrador.
- API de administración: un registro de aplicación con App Role `teams.areas.admin` asignado a los administradores. Su audiencia (`api://…`) va en `ADMIN_API_AUDIENCE`.

## API de administración

Disponible sólo en modo áreas. Requiere `Authorization: Bearer <token de Entra>` con el rol de administración.

| Método | Ruta | Uso |
|---|---|---|
| GET | `/api/admin/channels` | Canales conocidos por el bot y el área a la que pertenecen |
| GET | `/api/admin/areas` | Lista de áreas |
| GET | `/api/admin/areas/{id}` | Detalle de un área |
| PUT | `/api/admin/areas/{id}` | Crea o reemplaza un área |
| POST | `/api/admin/areas/{id}/disable` · `/enable` | Deshabilita o habilita (no se borran áreas) |

Ejemplo de cuerpo para `PUT /api/admin/areas/rrhh`:

```json
{
  "displayName": "Recursos Humanos",
  "accessMode": "any",
  "requiredGroups": ["<object-id grupo Buk Liquid>", "<object-id grupo Buk Asiss>"],
  "channels": [
    { "alias": "rrhh_general", "conversationId": "19:...@thread.tacv2", "displayName": "General" }
  ],
  "agent": { "triggerUrlSecret": "agent-rrhh-trigger-url", "tokenSecret": "agent-rrhh-token" },
  "enabled": true,
  "allowPublishWithoutRequest": false
}
```

Validaciones: los grupos son Object IDs, cada canal y alias pertenece a una sola área, los canales deben haber recibido al menos un mensaje del bot y los dos secretos deben existir en Key Vault.

## Desarrollo y pruebas

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-dev.txt
python -m pytest -q
python src/main.py
```

## Despliegue

- `main_teams-adapter.yml`: producción (`teams-adapter`). Sólo despliega desde `main`.
- `dev_teams-adapter.yml`: DEV. Corre pruebas en ramas `feature/**` y despliega si existen la variable `TEAMS_ADAPTER_DEV_APP` y el secreto `AZURE_WEBAPP_PUBLISH_PROFILE_DEV`.
