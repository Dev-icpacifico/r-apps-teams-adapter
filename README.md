# Teams Agent Adapter

Backend que conecta Microsoft Teams con agentes de espacio de trabajo. Recibe mensajes del bot (`/api/messages`), invoca al agente y entrega sus respuestas (`/api/teams-callback`) y publicaciones (`/api/teams-publish`) que llegan desde el MCP de Teams.

## Modos de operación

| Modo | Activación | Comportamiento |
|---|---|---|
| Heredado | `TEAMS_AREAS_ENABLED` vacío o `false` | Un solo agente (`AGENT_TRIGGER_URL`, `AGENT_ACCESS_TOKEN`) y destinos de la tabla `TeamsDestinations`. Es el comportamiento original. |
| Multiagente por áreas | `TEAMS_AREAS_ENABLED=true` | Cada canal pertenece a un área; el área define los agentes que atienden, cada uno con un alias, y las reglas de grupos de Entra ID para usarlos (del área y de cada agente). |

### Flujo por áreas

1. Un usuario menciona al bot en un canal del área.
2. Se busca el área del canal en `TeamsAreas`. Si no hay área habilitada, se responde que el canal no está habilitado.
3. Se consulta en Microsoft Graph (`/groups/{id}/transitiveMembers`, pertenencia transitiva, solo con `GroupMember.Read.All`) a qué grupos de las reglas del área pertenece el usuario, en una sola pasada. Ante un error se deniega.
4. Se calcula qué agentes puede usar (ver [Reglas de permisos](#reglas-de-permisos)). Si no puede usar ninguno, se responde que no tiene permisos.
5. Se elige el agente a partir del texto (sin la mención al bot):
   - `@Bot agentes` (o `ayuda`, o sólo la mención): lista **los agentes que el usuario puede usar**; no invoca a ninguno.
   - `@Bot <alias> <solicitud>` (también `alias: …`): ese agente recibe la solicitud sin el alias. Si el usuario no tiene permiso para ese agente, se le avisa y se le indican los agentes que sí puede usar; no se invoca nada.
   - Sin alias: lo atiende `defaultAgent` si el usuario puede usarlo; si no, el único agente permitido; si tiene varios, se pide elegir.
   - Un alias de otra área no se reconoce.
6. Se crea una correlación con el área y el agente, y se invoca al agente por su canal API (`POST {AGENT_TRIGGER_BASE_URL}/{triggerId}/trigger`) con:
   - el token común (`AGENT_DEFAULT_TOKEN_SECRET`) o el `tokenSecret` propio del agente, leído desde Key Vault;
   - `conversation_key` estable por hilo y agente, para continuar la conversación en el mismo hilo;
   - `Idempotency-Key` derivado del ID del mensaje de Teams, para no disparar dos veces el mismo mensaje.
7. El agente responde con `enviar_respuesta_teams` (una respuesta por solicitud) y puede publicar con `publicar_mensaje_teams` **sólo en canales de la misma área**, enviando el `correlation_id`.
8. Publicar sin `correlation_id` sólo se permite si el área tiene `allowPublishWithoutRequest=true`.

Chats personales y de grupo no se atienden en este modo.

### Publicación directa desde ChatGPT

Para publicar sin una solicitud de Teams (tareas programadas, boletines o un pedido hecho en ChatGPT), el MCP ofrece la herramienta `publicar_en_canal_teams`, que llama a `POST /api/teams-publish-direct`. Requisitos:

- El área define `directPublish`: grupos de publicadores (`requiredGroups`, `accessMode`) y los alias de canal que admiten publicaciones directas (`channels`). Sin esa regla, el área no acepta publicaciones directas.
- Quien publica se identifica con el `oid` de su token de Entra. APIM lo toma del token ya validado y lo inyecta en la llamada, sobrescribiendo cualquier valor que envíe el cliente. La Function lo reenvía como `caller_oid`.
- El adaptador verifica en Graph que ese usuario cumpla la regla y registra cada publicación con área, canal y `publicador_oid`.

`allowPublishWithoutRequest` sigue existiendo, pero abre el área a cualquier llamada sin identificar. Se recomienda `directPublish` en su lugar.

### Reglas de permisos

Cada regla es una lista de grupos de Entra ID (`requiredGroups`, Object IDs) con un modo (`accessMode`): `any` = al menos uno, `all` = todos. Hay dos niveles, ambos opcionales:

| Nivel | Efecto |
|---|---|
| Área | Filtro común: si existe, todo usuario debe cumplirlo para usar cualquier agente del área. |
| Agente | Requisito propio del agente. Sin regla propia, el agente queda disponible para quien cumpla la del área. |

Para usar un agente hay que cumplir las reglas existentes de ambos niveles. Un agente sin regla en un área sin regla se rechaza al guardar: no hay agentes abiertos.

Ejemplo, área sin regla común y un grupo por agente: quien sólo está en el grupo de Vambe ve y usa únicamente `vambe`; si además entra al grupo de Meta, también puede usar `metaads`. Publicar en otro canal del área lo hace el agente durante una solicitud ya autorizada, por lo que no hay un permiso aparte para publicar.

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
| GET | `/api/admin/teams` | Equipos con la app instalada y sus canales, indicando área y alias si ya están asignados |
| POST | `/api/admin/teams/sync` | Vuelve a leer los canales de los equipos conocidos (cuerpo opcional `{"teamId": "…"}`) |
| GET | `/api/admin/channels` | Lista plana de canales conocidos y su área |
| GET | `/api/admin/areas` | Lista de áreas |
| GET | `/api/admin/areas/{id}` | Detalle de un área |
| PUT | `/api/admin/areas/{id}` | Crea o reemplaza un área completa |
| PUT · DELETE | `/api/admin/areas/{id}/channels/{alias}` | Agrega o reemplaza un canal (`{"conversationId": "19:…", "displayName"?}`), o lo quita |
| PUT · DELETE | `/api/admin/areas/{id}/agents/{alias}` | Agrega o actualiza un agente (sólo los campos enviados), o lo quita |
| POST | `/api/admin/areas/{id}/disable` · `/enable` | Deshabilita o habilita (no se borran áreas) |

Las ediciones parciales validan el área completa con las mismas reglas que el `PUT`. Al quitar un canal también se quita de `directPublish`; al quitar el agente predeterminado se limpia `defaultAgent`.

### Descubrimiento de equipos y canales

Al instalar la app en un equipo, el bot recibe el evento de instalación y registra **todos** los canales del equipo en `TeamsKnownChannels` (API de Teams del bot, sin permisos de Graph). Los eventos de canal creado, renombrado o eliminado, y de equipo renombrado, mantienen el registro al día. Ya no hace falta mencionar al bot en cada canal antes de asignarlo a un área. Para equipos donde la app se instaló antes de esta función, se usa `POST /api/admin/teams/sync`; sólo se pueden sincronizar equipos de los que el bot ya conoce al menos un canal.

Cada canal guarda su tipo (`standard`, `private` o `shared`), según lo informa la API de Teams. Los canales **privados** no admiten el bot: la app instalada en el equipo no está presente en ellos, no recibe menciones y no puede publicar. Por eso `GET /api/admin/teams` y `GET /api/admin/channels` los omiten salvo `?includePrivate=true`, y el adaptador rechaza asignarlos a un área. La API del bot ve todos los canales del equipo, incluso privados de los que el administrador no es miembro.

El script `scripts/AgentesTeams.ps1` envuelve estas operaciones para administradores (ver su ayuda).

Ejemplo de cuerpo para `PUT /api/admin/areas/rrhh`:

```json
{
  "displayName": "Recursos Humanos",
  "accessMode": "any",
  "requiredGroups": ["<object-id grupo común del área (opcional)>"],
  "channels": [
    { "alias": "rrhh_general", "conversationId": "19:...@thread.tacv2", "displayName": "General" }
  ],
  "agents": [
    {
      "alias": "asistencia",
      "displayName": "Asistente de asistencia",
      "description": "Informes de asistencia y licencias",
      "triggerId": "agtch_...",
      "accessMode": "any",
      "requiredGroups": ["<object-id grupo Buk Asiss>"]
    },
    {
      "alias": "liquidaciones",
      "displayName": "Analista de liquidaciones",
      "triggerId": "agtch_...",
      "tokenSecret": "agent-liquidaciones-token",
      "accessMode": "all",
      "requiredGroups": ["<object-id grupo Buk Liquid>", "<object-id grupo Finanzas>"]
    }
  ],
  "defaultAgent": "asistencia",
  "enabled": true,
  "allowPublishWithoutRequest": false,
  "directPublish": {
    "accessMode": "any",
    "requiredGroups": ["<object-id grupo de publicadores>"],
    "channels": ["rrhh_general"]
  }
}
```

Validaciones:
- los grupos son Object IDs; `accessMode` es `any` (por defecto) o `all`, en el área y en cada agente;
- si el área no tiene `requiredGroups`, todos sus agentes deben tener los suyos;
- cada canal y alias de canal pertenece a una sola área;
- los canales deben ser conocidos por el bot (app instalada en el equipo o equipo sincronizado);
- cada agente tiene alias (`a-z`, `0-9`, `_`, `-`; no `agentes`, `ayuda` ni `help`), `displayName` y `triggerId` (`agtch_…`, el ID del canal API del agente);
- `tokenSecret` es opcional; sin él se usa `AGENT_DEFAULT_TOKEN_SECRET`. Todos los secretos usados deben existir en Key Vault;
- `defaultAgent` es opcional y debe ser uno de los alias;
- `directPublish` es opcional; si existe, necesita al menos un grupo y al menos un canal del área.

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
