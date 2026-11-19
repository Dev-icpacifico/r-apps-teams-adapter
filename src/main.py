import asyncio
import base64
import dataclasses
import logging
import os
import uuid

import httpx
import uvicorn
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.data.tables.aio import TableServiceClient
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from microsoft_teams.api import (
    Account,
    Attachment,
    ConversationUpdateActivity,
    InstalledActivity,
    MessageActivity,
    MessageActivityInput,
    TypingActivityInput,
)
from microsoft_teams.api.activities.utils.strip_mentions_text import (
    StripMentionsTextOptions,
    strip_mentions_text,
)
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter

from admin_api import build_admin_router
from admin_auth import EntraAdminValidator
from agents import AgentInvoker, SecretResolver, build_agent_input, post_to_agent
from area_service import AreaService, IncomingMessage, RequestDenied
from channel_names import ChannelNameResolver, TeamNames
from access_requests import CARD_CONTENT_TYPE, SUBMIT_ACTION, RequestStore, request_summary
from admin_review import REVIEW_ACTION, AdminChatStore
from agent_catalog import AgentCatalogStore
from channel_sync import ChannelSync, TeamSnapshot
from mentions import bot_display_name
from config import Settings
from graph_groups import DenyAllGroups, GraphGroupChecker
from stores import AreaStore, CorrelationStore, KnownChannelStore
from pdf_render import PdfContentError, render_pdf, safe_filename
from file_transfer import (
    FILE_CONSENT_TYPE,
    FILE_INFO_TYPE,
    FileValidationError,
    PendingFileStore,
    consent_accepted,
    make_pending,
    validate_file,
)
from teams_people import (
    GraphPeople,
    InvalidIdentifier,
    PersonNotFound,
    RosterDirectory,
    TeamMember,
    CHANNEL_MENTION_WORDS,
    apply_channel_mention,
    apply_mentions,
    normalize_identifier,
    parse_identifiers,
)


load_dotenv()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("teams-adapter")


# ============================================================
# FASTAPI + TEAMS SDK
# ============================================================

fastapi_app = FastAPI()
adapter = FastAPIAdapter(app=fastapi_app)
app = App(http_server_adapter=adapter)


# ============================================================
# CONFIGURACIÓN
# ============================================================

settings = Settings.from_env()

TABLE_PARTITION_KEY = "TeamsConversation"


# ============================================================
# AZURE TABLE STORAGE
# ============================================================

storage_credential = DefaultAzureCredential()

table_service_client = TableServiceClient(
    endpoint=f"https://{settings.storage_account_name}.table.core.windows.net",
    credential=storage_credential
)

table_client = table_service_client.get_table_client(
    table_name=settings.correlations_table
)

destinations_table_client = table_service_client.get_table_client(
    table_name=settings.destinations_table
)

correlations = CorrelationStore(
    table_client,
    ttl_hours=settings.correlation_ttl_hours
)


# ============================================================
# MODO MULTIAGENTE POR ÁREAS (opcional)
# ============================================================

area_service: AreaService | None = None
channel_sync: ChannelSync | None = None
pending_files: PendingFileStore | None = None
graph_people: GraphPeople | None = None
graph_credential = None
secret_client = None

if settings.areas_enabled:
    if settings.key_vault_uri:
        from azure.keyvault.secrets.aio import SecretClient

        secret_client = SecretClient(
            vault_url=settings.key_vault_uri,
            credential=storage_credential
        )

    if settings.tenant_id and settings.client_id and settings.client_secret:
        graph_credential = ClientSecretCredential(
            tenant_id=settings.tenant_id,
            client_id=settings.client_id,
            client_secret=settings.client_secret
        )
    else:
        logger.error(
            "Modo áreas sin TENANT_ID/CLIENT_ID/CLIENT_SECRET: "
            "no se podrán verificar grupos y se denegará el acceso"
        )

    _secrets = SecretResolver(secret_client) if secret_client else None
    known_channels = KnownChannelStore(
        table_service_client.get_table_client(table_name=settings.channels_table)
    )
    channel_sync = ChannelSync(known_channels, lambda team_id, url: fetch_team_snapshot(team_id, url))
    area_service = AreaService(
        areas=AreaStore(
            table_service_client.get_table_client(table_name=settings.areas_table),
            cache_seconds=settings.areas_cache_seconds
        ),
        channels=known_channels,
        correlations=correlations,
        groups=(
            GraphGroupChecker(graph_credential, cache_seconds=settings.group_cache_seconds)
            if graph_credential else DenyAllGroups()
        ),
        agents=AgentInvoker(
            _secrets,
            default_token_secret=settings.agent_default_token_secret,
            base_url=settings.agent_trigger_base_url,
            teams_plugin_name=settings.agent_teams_plugin_name
        ),
        secrets=_secrets,
        catalog=AgentCatalogStore(
            table_service_client.get_table_client(table_name=settings.areas_table),
            cache_seconds=settings.areas_cache_seconds,
        ),
        requests=RequestStore(
            table_service_client.get_table_client(table_name=settings.areas_table)
        ),
        admin_chats=AdminChatStore(
            table_service_client.get_table_client(table_name=settings.areas_table)
        ),
        admin_group=settings.teams_admin_group_id,
    )
    pending_files = PendingFileStore(
        table_service_client.get_table_client(table_name=settings.areas_table)
    )
    graph_people = GraphPeople(graph_credential) if graph_credential else None

    fastapi_app.include_router(
        build_admin_router(
            area_service,
            EntraAdminValidator(
                tenant_id=settings.tenant_id,
                audiences=settings.admin_audiences,
                required_role=settings.admin_required_role
            ),
            channel_sync,
            notify=lambda conversation_id, text: app.send(conversation_id, text),
        )
    )

    logger.info("Modo multiagente por áreas habilitado")


@fastapi_app.get("/health/storage")
async def health_storage():
    try:
        async for _ in table_client.query_entities(
            query_filter="PartitionKey eq @pk",
            parameters={"pk": TABLE_PARTITION_KEY},
            results_per_page=1
        ):
            break

        return {
            "status": "ok",
            "storage": "connected",
            "table": settings.correlations_table,
            "areas_enabled": settings.areas_enabled
        }

    except AzureError as exc:
        logging.exception("Storage health check failed")

        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "storage": "unavailable",
                "error_type": type(exc).__name__
            }
        )


# ============================================================
# MODO HEREDADO: destinos y correlaciones
# ============================================================

async def get_destination(
    alias: str
):
    return await destinations_table_client.get_entity(
        partition_key="TeamsDestination",
        row_key=alias
    )


async def legacy_publish_to_teams(
    alias: str,
    message: str
) -> None:

    destination = await get_destination(alias)

    if not destination.get("enabled", False):
        raise RequestDenied(
            403,
            f"Destino Teams deshabilitado: {alias}"
        )

    conversation_id = destination.get(
        "conversation_id"
    )

    if not conversation_id:
        raise RequestDenied(
            403,
            f"Destino Teams sin conversation_id: {alias}"
        )

    await app.send(
        conversation_id,
        message
    )


def get_channel_conversation_id(
    conversation_id: str
) -> str:

    if ";messageid=" in conversation_id:
        return conversation_id.split(";messageid=", 1)[0]

    return conversation_id


# ============================================================
# PUBLICACIÓN DESDE MCP
# ============================================================

@fastapi_app.post("/api/teams-publish")
async def teams_publish(request: Request):

    publish_key = request.headers.get("x-publish-key")

    if not settings.teams_publish_key:
        logger.error("TEAMS_PUBLISH_KEY no está configurada")

        return JSONResponse(
            status_code=500,
            content={"error": "Configuración interna incompleta"}
        )

    if publish_key != settings.teams_publish_key:
        logger.warning("Intento de publicación Teams no autorizado")

        return JSONResponse(
            status_code=401,
            content={"error": "No autorizado"}
        )

    try:
        body = await request.json()

    except Exception:
        return JSONResponse(
            status_code=400,
            content={"error": "JSON inválido"}
        )

    destino = body.get("destino")
    mensaje = body.get("mensaje")
    correlation_id = body.get("correlation_id") or None
    menciones = body.get("menciones")

    if not destino or not mensaje:
        return JSONResponse(
            status_code=400,
            content={"error": "Faltan destino o mensaje"}
        )

    try:
        if area_service is not None:
            area, channel = await area_service.resolve_publish(
                destino,
                correlation_id
            )
            await app.send(
                channel.conversation_id,
                await text_with_mentions(mensaje, menciones, channel.conversation_id)
            )
            logger.info(
                "Mensaje publicado en Teams | area=%s | alias=%s",
                area.id,
                channel.alias
            )
        else:
            await legacy_publish_to_teams(
                alias=destino,
                message=mensaje
            )
            logger.info(
                "Mensaje publicado en Teams | alias=%s",
                destino
            )

    except ResourceNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"error": "Destino Teams no encontrado"}
        )

    except MentionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    except RequestDenied as exc:
        logger.warning(
            "Publicación rechazada | alias=%s | motivo=%s",
            destino,
            exc.message
        )

        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.message}
        )

    except AzureError:
        logger.exception(
            "Error consultando destino Teams | alias=%s",
            destino
        )

        return JSONResponse(
            status_code=503,
            content={"error": "No fue posible consultar el destino Teams"}
        )

    except Exception:
        logger.exception(
            "Error publicando mensaje en Teams | alias=%s",
            destino
        )

        return JSONResponse(
            status_code=502,
            content={"error": "No fue posible publicar en Teams"}
        )

    return {
        "status": "sent",
        "destination": destino
    }


@fastapi_app.post("/api/teams-publish-direct")
async def teams_publish_direct(request: Request):
    """Publicación iniciada desde ChatGPT, sin solicitud de Teams.

    La autoriza la identidad de Entra de quien llama al MCP (caller_oid,
    inyectada por APIM desde el token validado) contra la regla
    directPublish del área. Sólo en modo áreas.
    """
    if not settings.teams_publish_key:
        logger.error("TEAMS_PUBLISH_KEY no está configurada")
        return JSONResponse(status_code=500, content={"error": "Configuración interna incompleta"})

    if request.headers.get("x-publish-key") != settings.teams_publish_key:
        logger.warning("Intento de publicación directa no autorizado")
        return JSONResponse(status_code=401, content={"error": "No autorizado"})

    if area_service is None:
        return JSONResponse(
            status_code=404, content={"error": "Publicación directa no disponible"}
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "JSON inválido"})
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "JSON inválido"})

    destino = body.get("destino")
    mensaje = body.get("mensaje")
    caller_oid = body.get("caller_oid") or ""
    if not isinstance(destino, str) or not isinstance(mensaje, str) or not destino or not mensaje:
        return JSONResponse(status_code=400, content={"error": "Faltan destino o mensaje"})

    try:
        area, channel = await area_service.resolve_direct_publish(destino, str(caller_oid))
        await app.send(
            channel.conversation_id,
            await text_with_mentions(mensaje, body.get("menciones"), channel.conversation_id),
        )
    except MentionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
    except RequestDenied as exc:
        logger.warning(
            "Publicación directa rechazada | alias=%s | motivo=%s", destino, exc.message
        )
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
    except AzureError:
        logger.exception("Error consultando destino Teams | alias=%s", destino)
        return JSONResponse(
            status_code=503, content={"error": "No fue posible consultar el destino Teams"}
        )
    except Exception:
        logger.exception("Error en publicación directa | alias=%s", destino)
        return JSONResponse(status_code=502, content={"error": "No fue posible publicar en Teams"})

    logger.info(
        "Publicación directa | area=%s | alias=%s | publicador_oid=%s",
        area.id, channel.alias, str(caller_oid).lower(),
    )
    return {"status": "sent", "destination": channel.alias}


# ============================================================
# PERSONAS: MENCIONES, MENSAJES DIRECTOS Y ARCHIVOS
# ============================================================

class MentionError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


async def fetch_team_members(team_id: str, service_url: str) -> list[TeamMember]:
    api = app.api.from_service_url(service_url) if service_url else app.api
    members = await api.conversations.get_members(team_id)
    return [
        TeamMember(
            teams_id=m.id,
            aad_object_id=(m.aad_object_id or "").lower(),
            name=m.name or "",
            email=getattr(m, "email", None) or "",
            upn=getattr(m, "user_principal_name", None) or "",
            team_id=team_id,
            service_url=service_url,
            is_guest=(getattr(m, "user_role", None) or "").lower() == "guest",
        )
        for m in members
        if m.id and m.id.startswith("29:")
    ]


people = RosterDirectory(fetch_team_members)
async def text_with_mentions(text: str, menciones, conversation_id: str):
    """Texto tal cual si no hay menciones; si las hay, actividad con
    ``<at>Nombre</at>`` y sus entidades. Sólo se puede mencionar a miembros
    del equipo del canal de destino."""
    tokens = menciones if isinstance(menciones, list) else str(menciones or "").replace(";", ",").split(",")
    tokens = [str(t).strip() for t in tokens if str(t).strip()]
    mention_channel = any(t.lower().lstrip("@") in CHANNEL_MENTION_WORDS for t in tokens)
    try:
        identifiers = parse_identifiers(
            [t for t in tokens if t.lower().lstrip("@") not in CHANNEL_MENTION_WORDS]
        )
    except InvalidIdentifier as exc:
        raise MentionError(400, str(exc)) from exc
    if not identifiers and not mention_channel:
        return text
    if area_service is None:
        raise MentionError(400, "Las menciones requieren el modo multiagente por áreas")
    members = []
    if identifiers:
        teams = await area_service.team_of_conversation(conversation_id)
        if not teams:
            raise MentionError(409, "No conozco el equipo del canal de destino para resolver menciones")
        try:
            members = await people.find_all(identifiers, teams)
        except PersonNotFound as exc:
            raise MentionError(
                422, "Estas personas no son miembros del equipo del canal: " + ", ".join(exc.identifiers)
            ) from exc
    text = apply_mentions(text, members)
    channel_id = (conversation_id or "").split(";messageid=", 1)[0]
    channel_name = ""
    if mention_channel:
        known = await area_service.channels.get(channel_id)
        channel_name = known.channel_name if known is not None else ""
        if not channel_name:
            raise MentionError(409, "No conozco el canal de destino para mencionarlo")
        text = apply_channel_mention(text, channel_name)
    activity = MessageActivityInput(text=text)
    for m in members:
        activity.add_mention(Account(id=m.teams_id, name=m.name), text=m.name, add_text=False)
    if mention_channel:
        activity.add_mention(Account(id=channel_id, name=channel_name, type="channel"), text=channel_name, add_text=False)
    logger.info("Mensaje con menciones | personas=%s | canal=%s", len(members), mention_channel)
    return activity


async def create_personal_chat(member: TeamMember, tenant_id: str) -> tuple[str, str]:
    """Crea (o recupera) el chat 1:1 del bot con la persona. Funciona si la
    persona es miembro de un equipo donde está instalada la app."""
    service_url = (member.service_url or "").rstrip("/") + "/"
    api = app.api.from_service_url(service_url)
    tenant = tenant_id or settings.tenant_id or ""
    response = await api.conversations.http.post(
        f"{service_url}v3/conversations",
        json={
            "bot": {"id": app.id},
            "members": [{"id": member.teams_id}],
            "isGroup": False,
            "tenantId": tenant,
            "channelData": {"tenant": {"id": tenant}},
        },
    )
    return str(response.json()["id"]), service_url


async def resolve_personal_chat(scope, identifier: str) -> tuple[str, str | None, str, str]:
    """(conversation_id, service_url, oid, nombre) del chat personal con la
    persona, verificando que pueda recibir el mensaje."""
    member = await people.find(identifier, scope.teams)
    if member is not None:
        if member.is_guest:
            raise RequestDenied(403, "Sólo se puede enviar a personas de la empresa (no a invitados)")
        conversation_id, service_url = await create_personal_chat(member, scope.tenant_id)
        return conversation_id, service_url, member.aad_object_id, member.name

    # No está en ningún equipo con la app: Entra + instalación proactiva.
    info = await graph_people.find(identifier) if graph_people is not None else None
    if info is None:
        raise RequestDenied(
            404, "No encontré a la persona en los equipos donde está instalada la app"
        )
    if info.get("is_guest"):
        raise RequestDenied(403, "Sólo se puede enviar a personas de la empresa (no a invitados)")
    if not settings.teams_catalog_app_id:
        raise RequestDenied(
            409, "La persona no tiene la app instalada y la instalación automática no está configurada"
        )
    chat_id = await graph_people.install_app_for_user(info["id"], settings.teams_catalog_app_id)
    if not chat_id:
        raise RequestDenied(409, "No se pudo instalar la app para la persona")
    return chat_id, None, info["id"], info["name"]


async def _sender_note(scope) -> str:
    """Quién envía y a pedido de quién (transparencia para quien recibe)."""
    requester = await people.find(scope.requester_oid, scope.teams)
    who = requester.name if requester is not None else "una persona de la empresa"
    where = f" en {scope.area.display_name}" if scope.area is not None else " desde ChatGPT"
    return f"_Enviado por {scope.agent.display_name} a pedido de {who}{where}_"


async def _publisher_body(request: Request):
    if not settings.teams_publish_key:
        return None, JSONResponse(status_code=500, content={"error": "Configuración interna incompleta"})
    if request.headers.get("x-publish-key") != settings.teams_publish_key:
        logger.warning("Intento de envío directo no autorizado")
        return None, JSONResponse(status_code=401, content={"error": "No autorizado"})
    if area_service is None:
        return None, JSONResponse(status_code=404, content={"error": "Disponible sólo en modo áreas"})
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse(status_code=400, content={"error": "JSON inválido"})
    if not isinstance(body, dict):
        return None, JSONResponse(status_code=400, content={"error": "JSON inválido"})
    return body, None


@fastapi_app.post("/api/teams-direct-message")
async def teams_direct_message(request: Request):
    """Mensaje al chat personal de una persona (sin que haya escrito antes).

    Puede recibirlo cualquier persona de la empresa (no invitados). Lo
    origina una solicitud de Teams (correlation_id) o, desde ChatGPT, quien
    llama (caller_oid) con acceso al agente indicado (agente).
    """
    body, error = await _publisher_body(request)
    if error is not None:
        return error
    mensaje = body.get("mensaje")
    if not isinstance(mensaje, str) or not mensaje.strip():
        return JSONResponse(status_code=400, content={"error": "Falta mensaje"})
    try:
        identifier = normalize_identifier(body.get("destinatario"))
        scope = await area_service.direct_message_scope(
            body.get("correlation_id") or None, str(body.get("caller_oid") or ""), str(body.get("agente") or "")
        )
        conversation_id, service_url, oid, name = await resolve_personal_chat(scope, identifier)
        await app.send(conversation_id, f"{await _sender_note(scope)}\n\n{mensaje}", service_url=service_url)
    except InvalidIdentifier as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except RequestDenied as exc:
        logger.warning("Mensaje directo rechazado | motivo=%s", exc.message)
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
    except Exception:
        logger.exception("Error enviando mensaje directo")
        return JSONResponse(status_code=502, content={"error": "No fue posible enviar el mensaje directo"})
    logger.info("Mensaje directo enviado | destinatario=%s | area=%s", oid, scope.area.id if scope.area else "")
    return {"status": "sent", "recipient": name}


@fastapi_app.post("/api/teams-file")
async def teams_file(request: Request):
    """Ofrece un archivo en el chat personal de una persona. Teams pide su
    consentimiento; al aceptar, el bot lo sube a su OneDrive."""
    body, error = await _publisher_body(request)
    if error is not None:
        return error
    try:
        identifier = normalize_identifier(body.get("destinatario"))
        scope = await area_service.direct_message_scope(
            body.get("correlation_id") or None, str(body.get("caller_oid") or ""), str(body.get("agente") or "")
        )
        if body.get("contenido_base64"):
            name, data = validate_file(
                body.get("nombre_archivo"), body.get("contenido_base64"), settings.max_file_bytes
            )
        else:
            # El bot arma el PDF con la plantilla fija a partir del texto.
            if str(body.get("formato") or "pdf").lower() != "pdf":
                raise FileValidationError("sólo se puede generar formato pdf")
            titulo = str(body.get("titulo") or "").strip()
            pdf = render_pdf(titulo, body.get("contenido"), scope.agent.display_name)
            name, data = validate_file(
                body.get("nombre_archivo") or safe_filename(titulo),
                base64.b64encode(pdf).decode(), settings.max_file_bytes,
            )
        conversation_id, service_url, oid, recipient = await resolve_personal_chat(scope, identifier)
        mensaje = str(body.get("mensaje") or "").strip()[:2000]
        note = await _sender_note(scope)
        pending = make_pending(name, data, oid, mensaje or name, note)
        await pending_files.save(pending)
        activity = MessageActivityInput(
            text=f"{note}\n\n{mensaje}" if mensaje else note
        ).add_attachments(Attachment(
            content_type=FILE_CONSENT_TYPE,
            name=name,
            content={
                "description": (mensaje or name)[:250],
                "sizeInBytes": len(data),
                "acceptContext": {"fileId": pending.id},
                "declineContext": {"fileId": pending.id},
            },
        ))
        await app.send(conversation_id, activity, service_url=service_url)
    except (FileValidationError, InvalidIdentifier, PdfContentError) as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    except RequestDenied as exc:
        logger.warning("Archivo rechazado | motivo=%s", exc.message)
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})
    except Exception:
        logger.exception("Error ofreciendo archivo")
        return JSONResponse(status_code=502, content={"error": "No fue posible enviar el archivo"})
    logger.info("Archivo ofrecido | id=%s | bytes=%s | destinatario=%s", pending.id, len(data), oid)
    return {"status": "offered", "recipient": recipient, "file": name,
            "note": "La persona debe aceptar el archivo en Teams para recibirlo."}


@app.on_file_consent
async def handle_file_consent(ctx) -> None:
    value = ctx.activity.value
    context = value.context if isinstance(value.context, dict) else {}
    file_id = str(context.get("fileId", ""))
    if pending_files is None:
        return
    pending = await pending_files.get(file_id)
    if pending is None:
        await ctx.send("Ese archivo ya no está disponible (venció o ya fue entregado).")
        return
    oid = (getattr(ctx.activity.from_, "aad_object_id", "") or "").lower()
    if oid != pending.recipient_oid:
        logger.warning("Consentimiento de archivo de otra persona | id=%s", file_id)
        return
    if not consent_accepted(value.action):
        await pending_files.delete(file_id)
        await ctx.send(f"Rechazaste el archivo **{pending.name}**; no se guardó.")
        logger.info("Archivo rechazado por la persona | id=%s", file_id)
        return
    info = value.upload_info
    size = len(pending.data)
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.put(
            info.upload_url,
            content=pending.data,
            headers={"Content-Length": str(size), "Content-Range": f"bytes 0-{size - 1}/{size}"},
        )
    if response.status_code not in (200, 201):
        logger.error("No se pudo subir el archivo | id=%s | status=%s", file_id, response.status_code)
        await ctx.send(f"No pude guardar **{pending.name}** en tu OneDrive. Inténtalo de nuevo más tarde.")
        return
    await ctx.send(MessageActivityInput().add_attachments(Attachment(
        content_type=FILE_INFO_TYPE,
        content_url=info.content_url,
        name=info.name,
        content={"uniqueId": info.unique_id, "fileType": info.file_type},
    )))
    await pending_files.delete(file_id)
    logger.info("Archivo entregado | id=%s | bytes=%s", file_id, size)


# ============================================================
# CALLBACK DESDE MCP
# ============================================================

@fastapi_app.post("/api/teams-callback")
async def teams_callback(request: Request):

    callback_key = request.headers.get("x-callback-key")

    if not settings.teams_callback_key:
        logger.error("TEAMS_CALLBACK_KEY no está configurada")
        return JSONResponse(
            status_code=500,
            content={"error": "Configuración interna incompleta"}
        )

    if callback_key != settings.teams_callback_key:
        logger.warning("Intento de callback no autorizado")
        return JSONResponse(
            status_code=401,
            content={"error": "No autorizado"}
        )

    try:
        body = await request.json()
    except Exception:
        logger.warning("Callback recibido con JSON inválido")
        return JSONResponse(
            status_code=400,
            content={"error": "JSON inválido"}
        )

    respuesta = body.get("respuesta")
    correlation_id = body.get("correlation_id")
    menciones = body.get("menciones")

    if not respuesta or not correlation_id:
        return JSONResponse(
            status_code=400,
            content={"error": "Faltan respuesta o correlation_id"}
        )

    try:
        correlation = await correlations.get(correlation_id)

    except AzureError:
        logger.exception(
            "Error consultando Table Storage | correlation_id=%s",
            correlation_id
        )

        return JSONResponse(
            status_code=503,
            content={"error": "Servicio de correlación no disponible"}
        )

    if correlation is None:
        logger.warning(
            "correlation_id no encontrado | correlation_id=%s",
            correlation_id
        )

        return JSONResponse(
            status_code=404,
            content={"error": "correlation_id no encontrado"}
        )

    # Las menciones se resuelven antes de consumir la correlación, para que
    # un error en ellas no impida reintentar la respuesta.
    try:
        activity = await text_with_mentions(
            respuesta, menciones, correlation.channel_conversation_id or correlation.conversation_id
        )
    except MentionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    # Las correlaciones con área se conservan hasta su expiración para
    # validar publicaciones; sólo admiten una respuesta.
    area_scoped = bool(area_service is not None and correlation.area_id)

    if area_scoped:
        try:
            await area_service.authorize_callback(correlation_id)
        except RequestDenied as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"error": exc.message}
            )

    logger.info(
        "Callback recibido | correlation_id=%s",
        correlation_id
    )

    try:
        await app.send(
            correlation.conversation_id,
            activity
        )

    except Exception:
        logger.exception(
            "Error enviando respuesta a Teams | correlation_id=%s",
            correlation_id
        )

        # No eliminamos la correlación:
        # puede ser necesaria para diagnóstico o reintento.
        return JSONResponse(
            status_code=502,
            content={"error": "No fue posible enviar la respuesta a Teams"}
        )

    logger.info(
        "Respuesta enviada a Teams | correlation_id=%s",
        correlation_id
    )

    try:
        if area_scoped:
            await correlations.mark_responded(correlation_id)
        else:
            await correlations.delete(correlation_id)

    except ResourceNotFoundError:
        logger.warning(
            "Correlación ya no existe al cerrarla | correlation_id=%s",
            correlation_id
        )

    except AzureError:
        # La respuesta ya fue enviada a Teams.
        # No devolvemos error al MCP solo porque falle la limpieza.
        logger.exception(
            "Respuesta enviada, pero no se pudo cerrar la correlación | correlation_id=%s",
            correlation_id
        )

    return {
        "status": "sent",
        "correlation_id": correlation_id
    }


# ============================================================
# WORKSPACE AGENT (modo heredado)
# ============================================================

async def trigger_workspace_agent(
    mensaje: str,
    correlation_id: str
) -> int:

    if not settings.agent_trigger_url:
        raise RuntimeError(
            "AGENT_TRIGGER_URL no configurada"
        )

    if not settings.agent_access_token:
        raise RuntimeError(
            "AGENT_ACCESS_TOKEN no configurado"
        )

    return await post_to_agent(
        settings.agent_trigger_url,
        settings.agent_access_token,
        build_agent_input(mensaje, correlation_id, None)
    )


# ============================================================
# MENSAJES DESDE MICROSOFT TEAMS
# ============================================================

def build_incoming_message(activity) -> IncomingMessage:
    channel_data = getattr(activity, "channel_data", None)
    channel = getattr(channel_data, "channel", None)
    team = getattr(channel_data, "team", None)
    conversation = activity.conversation

    channel_conversation_id = None
    if channel is not None and getattr(channel, "id", None):
        channel_conversation_id = channel.id
    elif getattr(conversation, "conversation_type", None) == "channel":
        channel_conversation_id = get_channel_conversation_id(conversation.id)

    return IncomingMessage(
        text=activity.text or "",
        conversation_id=conversation.id,
        channel_conversation_id=channel_conversation_id,
        channel_name=getattr(channel, "name", None) or "",
        team_id=getattr(team, "id", None) or "",
        team_name=getattr(team, "name", None) or "",
        team_aad_group_id=getattr(team, "aad_group_id", None) or "",
        service_url=activity.service_url or "",
        tenant_id=getattr(conversation, "tenant_id", None) or "",
        user_id=activity.from_.id,
        aad_object_id=getattr(activity.from_, "aad_object_id", "") or "",
        bot_id=activity.recipient.id,
        channel_id=activity.channel_id,
        activity_id=activity.id or "",
        bot_name=bot_display_name(activity, settings.teams_bot_display_name),
        user_name=getattr(activity.from_, "name", None) or "",
        reply_to_id=getattr(activity, "reply_to_id", None) or "",
    )


async def fetch_team_names(team_id: str, service_url: str) -> TeamNames:
    api = app.api.from_service_url(service_url) if service_url else app.api
    details = await api.teams.get_by_id(team_id)
    channels = await api.teams.get_conversations(team_id)
    return TeamNames(
        team_name=details.name or "",
        channels={c.id: c.name or "" for c in channels if c.id},
    )


async def fetch_team_snapshot(team_id: str, service_url: str) -> TeamSnapshot:
    api = app.api.from_service_url(service_url) if service_url else app.api
    details = await api.teams.get_by_id(team_id)
    channels = await api.teams.get_conversations(team_id)
    return TeamSnapshot(
        team_id=team_id,
        team_name=details.name or "",
        aad_group_id=details.aad_group_id or "",
        channels=tuple(
            (c.id, c.name or "", str(getattr(c, "type", "") or "")) for c in channels if c.id
        ),
    )


def _team_of(activity) -> tuple[str, str]:
    team = getattr(getattr(activity, "channel_data", None), "team", None)
    return (getattr(team, "id", None) or "", getattr(activity, "service_url", None) or "")


if channel_sync is not None:

    @app.on_install_add
    async def handle_install_add(ctx: ActivityContext[InstalledActivity]) -> None:
        team_id, service_url = _team_of(ctx.activity)
        if team_id:
            logger.info("App instalada en un equipo | equipo=%s", team_id)
            await channel_sync.sync_safely(team_id, service_url)

    @app.on_channel_created
    async def handle_channel_created(ctx: ActivityContext[ConversationUpdateActivity]) -> None:
        await channel_sync.sync_safely(*_team_of(ctx.activity))

    @app.on_channel_renamed
    async def handle_channel_renamed(ctx: ActivityContext[ConversationUpdateActivity]) -> None:
        await channel_sync.sync_safely(*_team_of(ctx.activity))

    @app.on_channel_deleted
    async def handle_channel_deleted(ctx: ActivityContext[ConversationUpdateActivity]) -> None:
        await channel_sync.sync_safely(*_team_of(ctx.activity))

    @app.on_team_renamed
    async def handle_team_renamed(ctx: ActivityContext[ConversationUpdateActivity]) -> None:
        await channel_sync.sync_safely(*_team_of(ctx.activity))


def card_message(card: dict) -> MessageActivityInput:
    return MessageActivityInput().add_attachments(
        Attachment(content_type=CARD_CONTENT_TYPE, content=card)
    )


async def send_review(send, req, card=None, conversation_id="", service_url="") -> None:
    """Envía la tarjeta de revisión y guarda su referencia para bloquearla
    cuando la solicitud se resuelva. Si Teams la rechaza, envía el resumen
    en texto para que la solicitud no quede sin avisar."""
    card = card or await area_service.review_card(req)
    try:
        sent = await send(card_message(card))
    except Exception:  # noqa: BLE001
        logger.exception("Tarjeta de revisión rechazada; se envía texto | id=%s", req.id)
        await send(
            request_summary(req)
            + "\n\nNo pude mostrar la tarjeta con botones. Revisa con "
            + f"Get-AgentesSolicitud -Id {req.id}."
        )
        return
    activity_id = getattr(sent, "id", "") or ""
    if conversation_id and activity_id:
        try:
            await area_service.record_admin_card(req.id, {
                "conversationId": conversation_id, "activityId": activity_id, "serviceUrl": service_url,
            })
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo guardar la referencia de la tarjeta | id=%s", req.id)


async def notify_admins(req) -> None:
    """Envía la tarjeta de revisión al chat personal de cada administrador."""
    try:
        card = await area_service.review_card(req)
        targets = await area_service.admin_targets()
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo preparar el aviso a administradores | id=%s", req.id)
        return
    for chat in targets:
        try:
            await send_review(
                lambda activity, c=chat: app.send(c.conversation_id, activity), req, card,
                conversation_id=chat.conversation_id, service_url=chat.service_url,
            )
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo avisar al administrador | oid=%s", chat.aad_object_id)
    logger.info("Solicitud avisada a administradores | id=%s | chats=%s", req.id, len(targets))


async def lock_review_cards(req, clicked: dict | None = None) -> None:
    """Reemplaza todas las tarjetas de revisión de la solicitud (en el chat
    de cada administrador) por la versión resuelta, sin botones."""
    refs = list(req.admin_cards)
    if clicked and clicked.get("activityId") and not any(
        r.get("activityId") == clicked["activityId"] for r in refs
    ):
        refs.append(clicked)
    if not refs:
        return
    try:
        card = await area_service.review_card(req)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo preparar la tarjeta resuelta | id=%s", req.id)
        return
    locked = 0
    for ref in refs:
        try:
            await app.api.conversations.update_activity(
                ref["conversationId"], ref["activityId"], card_message(card),
                service_url=ref.get("serviceUrl") or None,
            )
            locked += 1
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo bloquear una tarjeta | id=%s | activity=%s", req.id, ref.get("activityId"))
    logger.info("Tarjetas de revisión bloqueadas | id=%s | %s/%s", req.id, locked, len(refs))


async def handle_personal_chat(ctx, incoming, value) -> None:
    if isinstance(value, dict) and value.get("accion") == REVIEW_ACTION:
        result = await area_service.review_from_card(dataclasses.replace(incoming, value=value))
        await ctx.send(result.message)
        req = result.request
        if req is None:
            return
        clicked = {
            "conversationId": incoming.conversation_id,
            "activityId": incoming.reply_to_id,
            "serviceUrl": incoming.service_url,
        }
        # También si ya estaba resuelta: así se bloquean tarjetas antiguas.
        await lock_review_cards(req, clicked)
        if not result.changed:
            return
        conversation_id = req.conversation.get("conversation_id", "")
        if conversation_id:
            message = f"La solicitud **{req.id}** fue **{req.status}**."
            if req.applied:
                message += "\n\n" + "\n".join(f"- {a}" for a in req.applied)
            if req.status == "aprobada" and req.people:
                message += "\n\nUn administrador asignará los permisos en Entra; te avisaremos al terminar."
            if req.review_comment:
                message += f"\n\nComentario: {req.review_comment}"
            try:
                await app.send(conversation_id, message)
            except Exception:  # noqa: BLE001
                logger.exception("No se pudo avisar al solicitante | id=%s", req.id)
        return

    text = (incoming.text or "").strip().lower()
    registration = await area_service.register_admin_chat(incoming)
    if "solicitudes" in text and await area_service.is_admin(incoming.aad_object_id):
        pending = await area_service.requests.list("pendiente") if area_service.requests else []
        if not pending:
            await ctx.send("No hay solicitudes pendientes.")
            return
        await ctx.send(f"Solicitudes pendientes: {len(pending)}" + (" (muestro las 10 más recientes)" if len(pending) > 10 else ""))
        for req in pending[:10]:
            await send_review(ctx.send, req, conversation_id=incoming.conversation_id,
                              service_url=incoming.service_url)
        return

    await ctx.send(registration)


channel_names = ChannelNameResolver(
    fetch_team_names,
    cache_seconds=settings.channel_names_cache_seconds
)


async def prepare_area_message(activity, incoming: IncomingMessage) -> IncomingMessage:
    """Quita la mención al bot y completa nombres de equipo y canal."""
    text = strip_mentions_text(
        activity,
        StripMentionsTextOptions(account_id=activity.recipient.id)
    )
    team_name, channel_name = incoming.team_name, incoming.channel_name
    if incoming.channel_conversation_id and (not team_name or not channel_name):
        fetched_team, fetched_channel = await channel_names.names(
            incoming.team_id,
            incoming.channel_conversation_id,
            incoming.service_url
        )
        team_name = team_name or fetched_team
        channel_name = channel_name or fetched_channel
    return dataclasses.replace(
        incoming,
        text=text if text is not None else incoming.text,
        team_name=team_name,
        channel_name=channel_name,
    )


@app.on_message
async def handle_message(
    ctx: ActivityContext[MessageActivity]
) -> None:

    await ctx.reply(
        TypingActivityInput()
    )

    logger.info(
        "Mensaje recibido desde Teams"
    )

    incoming = build_incoming_message(ctx.activity)

    if area_service is not None:
        value = getattr(ctx.activity, "value", None)
        is_personal = getattr(ctx.activity.conversation, "conversation_type", None) == "personal"

        if is_personal:
            await handle_personal_chat(ctx, incoming, value)
            return

        if isinstance(value, dict) and value.get("accion") == SUBMIT_ACTION:
            outcome = await area_service.submit_request(dataclasses.replace(incoming, value=value))
            await ctx.send(outcome.user_message)
            if outcome.request is not None:
                await notify_admins(outcome.request)
            if outcome.request is not None and settings.teams_requests_channel:
                try:
                    await app.send(
                        settings.teams_requests_channel,
                        "Nueva solicitud para revisar:\n\n" + request_summary(outcome.request),
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("No se pudo avisar la solicitud | id=%s", outcome.request.id)
            return

        incoming = await prepare_area_message(ctx.activity, incoming)
        outcome = await area_service.handle_incoming(incoming)
        if outcome.attachment is not None:
            await ctx.send(
                MessageActivityInput().add_attachments(
                    Attachment(
                        content_type=CARD_CONTENT_TYPE,
                        content=outcome.attachment["content"],
                    )
                )
            )
        else:
            await ctx.send(outcome.user_message)
        return

    correlation_id = str(
        uuid.uuid4()
    )

    try:
        await correlations.save(
            correlation_id,
            incoming.conversation_record()
        )

    except AzureError:
        logger.exception(
            "Error guardando correlación en Table Storage | correlation_id=%s",
            correlation_id
        )

        await ctx.send(
            "No pude iniciar el procesamiento de la solicitud."
        )
        return

    try:
        status_code = await trigger_workspace_agent(
            mensaje=incoming.text,
            correlation_id=correlation_id
        )

    except (httpx.HTTPError, RuntimeError):
        logger.exception(
            "Error al iniciar Workspace Agent | correlation_id=%s",
            correlation_id
        )

        await ctx.send(
            "Ocurrió un error al intentar procesar la solicitud."
        )
        return

    except Exception:
        logger.exception(
            "Error inesperado al iniciar Workspace Agent | correlation_id=%s",
            correlation_id
        )

        await ctx.send(
            "Ocurrió un error al intentar procesar la solicitud."
        )
        return

    if status_code == 202:
        await ctx.send(
            "Solicitud recibida. Estoy procesándola."
        )
        return

    logger.warning(
        "Workspace Agent respondió HTTP %s | correlation_id=%s",
        status_code,
        correlation_id
    )

    await ctx.send(
        "No pude iniciar el procesamiento de la solicitud."
    )


# ============================================================
# INICIO DE LA APLICACIÓN
# ============================================================

async def main():

    await app.initialize()

    port = int(
        os.getenv(
            "PORT",
            "3978"
        )
    )

    config = uvicorn.Config(
        app=fastapi_app,
        host="0.0.0.0",
        port=port
    )

    server = uvicorn.Server(
        config
    )

    try:
        await server.serve()
    finally:
        await table_service_client.close()
        await storage_credential.close()
        if graph_credential is not None:
            await graph_credential.close()
        if secret_client is not None:
            await secret_client.close()


if __name__ == "__main__":
    asyncio.run(main())
