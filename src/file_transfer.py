"""Envío de archivos por chat personal.

Teams sólo permite que un bot entregue un archivo en el chat personal con
consentimiento de la persona: el bot envía una tarjeta «¿Permitir?»; al
aceptarla, Teams entrega una URL de carga en el OneDrive de la persona, el
bot sube el archivo y publica la tarjeta del archivo. Hasta la respuesta, el
archivo queda en la tabla del adaptador (en partes, por el límite de 64 KiB
por propiedad) y vence a los ``PENDING_DAYS`` días.
"""

import base64
import binascii
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from azure.core.exceptions import ResourceNotFoundError

FILE_PARTITION_KEY = "TeamsPendingFile"
FILE_CONSENT_TYPE = "application/vnd.microsoft.teams.card.file.consent"
FILE_INFO_TYPE = "application/vnd.microsoft.teams.card.file.info"
MAX_FILE_BYTES = 4 * 1024 * 1024
PART_BYTES = 60_000
PARTS_PER_ENTITY = 15
PENDING_DAYS = 7
_NAME = re.compile(r'^[^\\/:*?"<>|\x00-\x1f]{1,180}\.[A-Za-z0-9]{1,10}$')


class FileValidationError(ValueError):
    pass


def validate_file(name: Any, content_b64: Any, max_bytes: int = MAX_FILE_BYTES) -> tuple[str, bytes]:
    name = str(name or "").strip()
    if not _NAME.match(name):
        raise FileValidationError("nombre de archivo inválido (debe tener extensión y no incluir rutas)")
    if not isinstance(content_b64, str) or not content_b64.strip():
        raise FileValidationError("falta el contenido del archivo en base64")
    text = content_b64.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise FileValidationError("el contenido no es base64 válido") from exc
    if not data:
        raise FileValidationError("el archivo está vacío")
    if len(data) > max_bytes:
        raise FileValidationError(f"el archivo supera {max_bytes // (1024 * 1024)} MB")
    return name, data


@dataclass
class PendingFile:
    id: str
    name: str
    size: int
    sha256: str
    recipient_oid: str
    description: str
    sender: str
    created_at: str
    data: bytes = b""


def new_file_id() -> str:
    return "F" + secrets.token_hex(12)


def consent_attachment(f: PendingFile) -> dict[str, Any]:
    return {
        "contentType": FILE_CONSENT_TYPE,
        "name": f.name,
        "content": {
            "description": f.description[:250] or f.name,
            "sizeInBytes": f.size,
            "acceptContext": {"fileId": f.id},
            "declineContext": {"fileId": f.id},
        },
    }


def file_info_attachment(upload_info) -> dict[str, Any]:
    return {
        "contentType": FILE_INFO_TYPE,
        "contentUrl": upload_info.content_url,
        "name": upload_info.name,
        "content": {"uniqueId": upload_info.unique_id, "fileType": upload_info.file_type},
    }


class PendingFileStore:
    def __init__(self, table_client):
        self._table = table_client

    async def save(self, f: PendingFile) -> None:
        parts = [f.data[i:i + PART_BYTES] for i in range(0, len(f.data), PART_BYTES)]
        groups = [parts[i:i + PARTS_PER_ENTITY] for i in range(0, len(parts), PARTS_PER_ENTITY)]
        for n, group in enumerate(groups):
            entity = {"PartitionKey": FILE_PARTITION_KEY, "RowKey": f"{f.id}-{n:03d}"}
            for k, part in enumerate(group):
                entity[f"p{k:02d}"] = part
            await self._table.upsert_entity(entity=entity)
        await self._table.upsert_entity(entity={
            "PartitionKey": FILE_PARTITION_KEY,
            "RowKey": f.id,
            "name": f.name,
            "size": f.size,
            "sha256": f.sha256,
            "recipient_oid": f.recipient_oid,
            "description": f.description,
            "sender": f.sender,
            "created_at": f.created_at,
            "chunks": len(groups),
        })

    async def get(self, file_id: str) -> PendingFile | None:
        if not re.match(r"^F[0-9a-f]{24}$", file_id or ""):
            return None
        try:
            meta = await self._table.get_entity(partition_key=FILE_PARTITION_KEY, row_key=file_id)
        except ResourceNotFoundError:
            return None
        created = str(meta.get("created_at", ""))
        try:
            expired = datetime.fromisoformat(created) + timedelta(days=PENDING_DAYS) < datetime.now(timezone.utc)
        except ValueError:
            expired = True
        if expired:
            await self.delete(file_id)
            return None
        data = b""
        for n in range(int(meta.get("chunks", 0))):
            chunk = await self._table.get_entity(partition_key=FILE_PARTITION_KEY, row_key=f"{file_id}-{n:03d}")
            for k in range(PARTS_PER_ENTITY):
                part = chunk.get(f"p{k:02d}")
                if part is None:
                    break
                data += bytes(part.value if hasattr(part, "value") else part)
        f = PendingFile(
            id=file_id, name=str(meta.get("name", "")), size=int(meta.get("size", 0)),
            sha256=str(meta.get("sha256", "")), recipient_oid=str(meta.get("recipient_oid", "")),
            description=str(meta.get("description", "")), sender=str(meta.get("sender", "")),
            created_at=created, data=data,
        )
        if hashlib.sha256(data).hexdigest() != f.sha256:
            raise ValueError("archivo pendiente corrupto")
        return f

    async def delete(self, file_id: str) -> None:
        try:
            meta = await self._table.get_entity(partition_key=FILE_PARTITION_KEY, row_key=file_id)
            chunks = int(meta.get("chunks", 0))
        except ResourceNotFoundError:
            return
        for n in range(chunks):
            try:
                await self._table.delete_entity(partition_key=FILE_PARTITION_KEY, row_key=f"{file_id}-{n:03d}")
            except ResourceNotFoundError:
                pass
        try:
            await self._table.delete_entity(partition_key=FILE_PARTITION_KEY, row_key=file_id)
        except ResourceNotFoundError:
            pass


def make_pending(name: str, data: bytes, recipient_oid: str, description: str, sender: str) -> PendingFile:
    return PendingFile(
        id=new_file_id(), name=name, size=len(data), sha256=hashlib.sha256(data).hexdigest(),
        recipient_oid=recipient_oid.lower(), description=description, sender=sender,
        created_at=datetime.now(timezone.utc).isoformat(), data=data,
    )
