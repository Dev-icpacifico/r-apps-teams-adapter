import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone

import httpx
import uvicorn
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.data.tables.aio import TableServiceClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from microsoft_teams.api import MessageActivity, TypingActivityInput
from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter


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

AGENT_TRIGGER_URL = os.getenv("AGENT_TRIGGER_URL")
AGENT_ACCESS_TOKEN = os.getenv("AGENT_ACCESS_TOKEN")
TEAMS_CALLBACK_KEY = os.getenv("TEAMS_CALLBACK_KEY")

STORAGE_ACCOUNT_NAME = os.getenv(
    "STORAGE_ACCOUNT_NAME",
    "stteamsadapterprod"
)

TEAMS_CORRELATIONS_TABLE = os.getenv(
    "TEAMS_CORRELATIONS_TABLE",
    "TeamsCorrelations"
)

TEAMS_DESTINATIONS_TABLE = os.getenv(
    "TEAMS_DESTINATIONS_TABLE",
    "TeamsDestinations"
)

TABLE_PARTITION_KEY = "TeamsConversation"


# ============================================================
# AZURE TABLE STORAGE
# ============================================================

storage_credential = DefaultAzureCredential()

table_service_client = TableServiceClient(
    endpoint=f"https://{STORAGE_ACCOUNT_NAME}.table.core.windows.net",
    credential=storage_credential
)

table_client = table_service_client.get_table_client(
    table_name=TEAMS_CORRELATIONS_TABLE
)

destinations_table_client = table_service_client.get_table_client(
    table_name=TEAMS_DESTINATIONS_TABLE
)

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
            "table": TEAMS_CORRELATIONS_TABLE
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


async def save_conversation(
    correlation_id: str,
    conversation: dict
) -> None:

    entity = {
        "PartitionKey": TABLE_PARTITION_KEY,
        "RowKey": correlation_id,
        "service_url": conversation.get("service_url", ""),
        "conversation_id": conversation.get("conversation_id", ""),
        "tenant_id": conversation.get("tenant_id", ""),
        "user_id": conversation.get("user_id", ""),
        "aad_object_id": conversation.get("aad_object_id", ""),
        "bot_id": conversation.get("bot_id", ""),
        "channel_id": conversation.get("channel_id", ""),
        "activity_id": conversation.get("activity_id", ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await table_client.upsert_entity(
        entity=entity
    )

    logger.info(
        "Conversación almacenada en Table Storage | correlation_id=%s",
        correlation_id
    )


async def get_conversation(
    correlation_id: str
):
    return await table_client.get_entity(
        partition_key=TABLE_PARTITION_KEY,
        row_key=correlation_id
    )


async def delete_conversation(
    correlation_id: str
) -> None:

    await table_client.delete_entity(
        partition_key=TABLE_PARTITION_KEY,
        row_key=correlation_id
    )

    logger.info(
        "Correlación eliminada de Table Storage | correlation_id=%s",
        correlation_id
    )

async def save_destination(
    alias: str,
    destination: dict
) -> None:

    entity = {
        "PartitionKey": "TeamsDestination",
        "RowKey": alias,
        "service_url": destination.get("service_url", ""),
        "conversation_id": destination.get("conversation_id", ""),
        "tenant_id": destination.get("tenant_id", ""),
        "bot_id": destination.get("bot_id", ""),
        "channel_id": destination.get("channel_id", ""),
        "enabled": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    await destinations_table_client.upsert_entity(
        entity=entity
    )

    logger.info(
        "Destino Teams almacenado | alias=%s",
        alias
    )


async def get_destination(
    alias: str
):
    return await destinations_table_client.get_entity(
        partition_key="TeamsDestination",
        row_key=alias
    )


async def publish_to_teams(
    alias: str,
    message: str
) -> None:

    destination = await get_destination(alias)

    if not destination.get("enabled", False):
        raise RuntimeError(
            f"Destino Teams deshabilitado: {alias}"
        )

    conversation_id = destination.get(
        "conversation_id"
    )

    if not conversation_id:
        raise RuntimeError(
            f"Destino Teams sin conversation_id: {alias}"
        )

    await app.send(
        conversation_id,
        message
    )

    logger.info(
        "Mensaje publicado en Teams | alias=%s",
        alias
    )


def get_channel_conversation_id(
    conversation_id: str
) -> str:

    if ";messageid=" in conversation_id:
        return conversation_id.split(";messageid=", 1)[0]

    return conversation_id


# ============================================================
@fastapi_app.post("/api/test-proactive-message")
async def test_proactive_message():

    alias = "transformacion_digital"

    try:
        await publish_to_teams(
            alias=alias,
            message="🧪 Prueba de publicación proactiva desde Teams Adapter."
        )

    except ResourceNotFoundError:
        return JSONResponse(
            status_code=404,
            content={"error": "Destino Teams no encontrado"}
        )

    except RuntimeError as exc:
        logger.warning(
            "Destino Teams no disponible | alias=%s | error=%s",
            alias,
            str(exc)
        )

        return JSONResponse(
            status_code=403,
            content={"error": str(exc)}
        )

    except AzureError:
        logger.exception(
            "Error consultando destino Teams | alias=%s",
            alias
        )

        return JSONResponse(
            status_code=503,
            content={"error": "No fue posible consultar el destino Teams"}
        )

    except Exception:
        logger.exception(
            "Error enviando mensaje proactivo | alias=%s",
            alias
        )

        return JSONResponse(
            status_code=502,
            content={"error": "No fue posible publicar en Teams"}
        )

    return {
        "status": "sent",
        "destination": alias
    }
    
    
# ============================================================

# ============================================================
# CALLBACK DESDE MCP
# ============================================================

@fastapi_app.post("/api/teams-callback")
async def teams_callback(request: Request):

    callback_key = request.headers.get("x-callback-key")

    if not TEAMS_CALLBACK_KEY:
        logger.error("TEAMS_CALLBACK_KEY no está configurada")
        return JSONResponse(
            status_code=500,
            content={"error": "Configuración interna incompleta"}
        )

    if callback_key != TEAMS_CALLBACK_KEY:
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
        conversation = await get_conversation(
            correlation_id
        )

    except ResourceNotFoundError:
        logger.warning(
            "correlation_id no encontrado | correlation_id=%s",
            correlation_id
        )

        return JSONResponse(
            status_code=404,
            content={"error": "correlation_id no encontrado"}
        )

    except AzureError:
        logger.exception(
            "Error consultando Table Storage | correlation_id=%s",
            correlation_id
        )

        return JSONResponse(
            status_code=503,
            content={"error": "Servicio de correlación no disponible"}
        )

    logger.info(
        "Callback recibido | correlation_id=%s",
        correlation_id
    )

    try:
        await app.send(
            conversation["conversation_id"],
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
        await delete_conversation(
            correlation_id
        )

    except ResourceNotFoundError:
        logger.warning(
            "Correlación ya no existe al intentar eliminarla | correlation_id=%s",
            correlation_id
        )

    except AzureError:
        # La respuesta ya fue enviada a Teams.
        # No devolvemos error al MCP solo porque falle la limpieza.
        logger.exception(
            "Respuesta enviada, pero no se pudo eliminar la correlación | correlation_id=%s",
            correlation_id
        )

    return {
        "status": "sent",
        "correlation_id": correlation_id
    }


# ============================================================
# WORKSPACE AGENT
# ============================================================

async def trigger_workspace_agent(
    mensaje: str,
    correlation_id: str
) -> int:

    if not AGENT_TRIGGER_URL:
        raise RuntimeError(
            "AGENT_TRIGGER_URL no configurada"
        )

    if not AGENT_ACCESS_TOKEN:
        raise RuntimeError(
            "AGENT_ACCESS_TOKEN no configurado"
        )

    payload = {
        "input": (
            f"{mensaje}\n\n"
            f"correlation_id: {correlation_id}"
        )
    }

    headers = {
        "Authorization": f"Bearer {AGENT_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }

    async with httpx.AsyncClient(
        timeout=30.0
    ) as client:

        response = await client.post(
            AGENT_TRIGGER_URL,
            json=payload,
            headers=headers
        )

    return response.status_code


# ============================================================
# MENSAJES DESDE MICROSOFT TEAMS
# ============================================================

@app.on_message
async def handle_message(
    ctx: ActivityContext[MessageActivity]
) -> None:

    await ctx.reply(
        TypingActivityInput()
    )

    mensaje = ctx.activity.text or ""
    if "registrar_destino_tdti" in mensaje.lower():

        destination = {
        "service_url": ctx.activity.service_url,
        "conversation_id": get_channel_conversation_id(
            ctx.activity.conversation.id
        ),
        "tenant_id": ctx.activity.conversation.tenant_id,
        "bot_id": ctx.activity.recipient.id,
        "channel_id": ctx.activity.channel_id,
    }

    try:
        await save_destination(
            alias="transformacion_digital",
            destination=destination
        )

        await ctx.send(
            "Canal registrado correctamente como destino "
            "'transformacion_digital'."
        )

    except AzureError:
        logger.exception(
            "Error registrando destino transformacion_digital"
        )

        await ctx.send(
            "No fue posible registrar este canal como destino."
        )

    return
        
    



    logger.info(
        "Mensaje recibido desde Teams"
    )

    correlation_id = str(
        uuid.uuid4()
    )

    conversation = {
        "service_url": ctx.activity.service_url,
        "conversation_id": ctx.activity.conversation.id,
        "tenant_id": ctx.activity.conversation.tenant_id,
        "user_id": ctx.activity.from_.id,
        "aad_object_id": getattr(
            ctx.activity.from_,
            "aad_object_id",
            ""
        ) or "",
        "bot_id": ctx.activity.recipient.id,
        "channel_id": ctx.activity.channel_id,
        "activity_id": ctx.activity.id,
    }

    try:
        await save_conversation(
            correlation_id=correlation_id,
            conversation=conversation
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
            mensaje=mensaje,
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


if __name__ == "__main__":
    asyncio.run(main())
