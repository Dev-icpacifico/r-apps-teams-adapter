import asyncio
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

from microsoft_teams.api import MessageActivity, TypingActivityInput
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter

from admin_api import build_admin_router
from admin_auth import EntraAdminValidator
from agents import AgentInvoker, SecretResolver, build_agent_input, post_to_agent
from area_service import AreaService, IncomingMessage, RequestDenied
from config import Settings
from graph_groups import DenyAllGroups, GraphGroupChecker
from stores import AreaStore, CorrelationStore, KnownChannelStore


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
    area_service = AreaService(
        areas=AreaStore(
            table_service_client.get_table_client(table_name=settings.areas_table),
            cache_seconds=settings.areas_cache_seconds
        ),
        channels=KnownChannelStore(
            table_service_client.get_table_client(table_name=settings.channels_table)
        ),
        correlations=correlations,
        groups=(
            GraphGroupChecker(graph_credential, cache_seconds=settings.group_cache_seconds)
            if graph_credential else DenyAllGroups()
        ),
        agents=AgentInvoker(_secrets),
        secrets=_secrets,
    )

    fastapi_app.include_router(
        build_admin_router(
            area_service,
            EntraAdminValidator(
                tenant_id=settings.tenant_id,
                audiences=settings.admin_audiences,
                required_role=settings.admin_required_role
            )
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
                mensaje
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
            respuesta
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
        outcome = await area_service.handle_incoming(incoming)
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
