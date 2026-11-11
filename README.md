# Teams Agent Adapter

Backend que conecta Microsoft Teams con agentes de espacio de trabajo. Recibe mensajes del bot (`/api/messages`), invoca al agente y entrega sus respuestas (`/api/teams-callback`) y publicaciones (`/api/teams-publish`) que llegan desde el MCP de Teams.

## Modos de operación

| Modo | Activación | Comportamiento |
|---|---|---|
| Heredado | `TEAMS_AREAS_ENABLED` vacío o `false` | Un solo agente (`AGENT_TRIGGER_URL`, `AGENT_ACCESS_TOKEN`) y destinos de la tabla `TeamsDestinations`. Es el comportamiento original. |
| Multiagente por áreas | `TEAMS_AREAS_ENABLED=true` | Cada canal pertenece a un área; el área define los grupos de Entra ID requeridos, el modo (`any`/`all`) y los agentes que atienden, cada uno con un alias. |

### Flujo por áreas

1. Un usuario menciona al bot en un canal. El canal queda registrado en `TeamsKnownChannels`, con el nombre del equipo y del canal obtenidos de la API de Teams.
2. Se busca el área del canal en `TeamsAreas`. Si no hay área habilitada, se responde que el canal no está habilitado.
3. Se verifica en Microsoft Graph (`/groups/{id}/transitiveMembers`, pertenencia transitiva, solo con `GroupMember.Read.All`) si el usuario cumple los grupos del área: `any` = al menos uno, `all` = todos. Ante un error se deniega.
4. Se elige el agente del área a partir del texto (sin la mención al bot):
   - `@Bot agentes` (o `ayuda`, o sólo la mención): lista los agentes del área; no invoca a ninguno.
   - `@Bot <alias> <solicitud>` (también `alias: …`): ese agente recibe la solicitud sin el alias.
   - Sin alias: si el área tiene un solo agente, o `defaultAgent`, lo atiende ese agente; si no, se pide elegir.
   - Un alias de otra área no se reconoce.
5. Se crea una correlación con el área y el agente, y se invoca al agente por su canal API (`POST {AGENT_TRIGGER_BASE_URL}/{triggerId}/trigger`) con:
   - el token común (`AGENT_DEFAULT_TOKEN_SECRET`) o el `tokenSecret` propio del agente, leído desde Key Vault;
   - `conversation_key` estable por hilo y agente, para continuar la conversación en el mismo hilo;
   - `Idempotency-Key` derivado del ID del mensaje de Teams, para no disparar dos veces el mismo mensaje.
6. El agente responde con `enviar_respuesta_teams` (una respuesta por solicitud) y puede publicar con `publicar_mensaje_teams` **sólo en canales de la misma área**, enviando el `correlation_id`.
7. Publicar sin `correlation_id` sólo se permite si el área tiene `allowPublishWithoutRequest=true`.

Chats personales y de grupo no se atienden en este modo.

Los comandos `agentes` y `ayuda` se declaran en `commandLists` del manifiesto de la app de Teams para que aparezcan al escribir `@Bot`. Teams no permite listas dinámicas en ese menú: la lista de agentes de cada área la entrega el propio bot.

## Variables de entorno

Comunes: `STORAGE_ACCOUNT_NAME`, `TEAMS_CORRELATIONS_TABLE`, `TEAMS_CALLBACK_KEY`, `TEAMS_PUBLISH_KEY`, `CLIENT_ID`, `CLIENT_SECRET`, `TENANT_ID`.

Heredado: `AGENT_TRIGGER_URL`, `AGENT_ACCESS_TOKEN`, `TEAMS_DESTINATIONS_TABLE`.

Áreas:

| Variable | Uso | Por defecto |
|---|---|---|
| `TEAMS_AREAS_ENABLED` | Activa el modo por áreas | `false` |
| `TEAMS_AREAS_TABLE` | Catálogo de áreas | `TeamsAreas` |
| `TEAMS_CHANNELS_TABLE` | Canales donde el bot recibió mensajes | `TeamsKnownChannels` |
| `KEY_VAULT_URI` | Key Vault con los tokens de los agentes | — |
| `AGENT_DEFAULT_TOKEN_SECRET` | Nombre del secreto con el token común (Workspace Agent access token) | — |
| `AGENT_TRIGGER_BASE_URL` | Base de la URL de disparo | `https://api.chatgpt.com/v1/workspace_agents` |
| `CHANNEL_NAMES_CACHE_SECONDS` | Caché de nombres de equipo y canal | `600` |
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
  "agents": [
    {
      "alias": "asistencia",
      "displayName": "Asistente de asistencia",
      "description": "Informes de asistencia y licencias",
      "triggerId": "agtch_..."
    },
    {
      "alias": "liquidaciones",
      "displayName": "Analista de liquidaciones",
      "triggerId": "agtch_...",
      "tokenSecret": "agent-liquidaciones-token"
    }
  ],
  "defaultAgent": "asistencia",
  "enabled": true,
  "allowPublishWithoutRequest": false
}
```

Validaciones:
- los grupos son Object IDs;
- cada canal y alias de canal pertenece a una sola área;
- los canales deben haber recibido al menos una mención al bot;
- cada agente tiene alias (`a-z`, `0-9`, `_`, `-`; no `agentes`, `ayuda` ni `help`), `displayName` y `triggerId` (`agtch_…`, el ID del canal API del agente);
- `tokenSecret` es opcional; sin él se usa `AGENT_DEFAULT_TOKEN_SECRET`. Todos los secretos usados deben existir en Key Vault;
- `defaultAgent` es opcional y debe ser uno de los alias.

El token de Workspace Agents es personal (ChatGPT Admin → Access token, alcance *Workspace Agents*) y requiere permiso sobre cada agente que dispara; si no lo tiene, el disparo responde 403.

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
