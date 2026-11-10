"""Validación de tokens de Entra ID para el endpoint de administración.

Se exige: firma válida del tenant, audiencia configurada y el App Role
de administración (por defecto teams.areas.admin) en el claim roles.
"""

import asyncio
import logging
from dataclasses import dataclass

import jwt

logger = logging.getLogger("teams-adapter.admin-auth")


class AdminAuthError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class AdminPrincipal:
    object_id: str
    name: str
    roles: tuple[str, ...]

    @property
    def actor(self) -> str:
        return self.name or self.object_id


class EntraAdminValidator:
    def __init__(self, tenant_id: str | None, audiences: list[str], required_role: str, jwks_client=None):
        self._tenant_id = tenant_id
        self._audiences = audiences
        self._required_role = required_role
        self._jwks = jwks_client or (
            jwt.PyJWKClient(
                f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys",
                cache_keys=True,
            )
            if tenant_id
            else None
        )

    @property
    def configured(self) -> bool:
        return bool(self._tenant_id and self._audiences and self._jwks)

    def _issuers(self) -> list[str]:
        return [
            f"https://login.microsoftonline.com/{self._tenant_id}/v2.0",
            f"https://sts.windows.net/{self._tenant_id}/",
        ]

    def validate(self, authorization: str | None) -> AdminPrincipal:
        if not self.configured:
            raise AdminAuthError(503, "Administración no configurada")
        if not authorization or not authorization.lower().startswith("bearer "):
            raise AdminAuthError(401, "Falta token Bearer")
        token = authorization[7:].strip()
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audiences,
                issuer=self._issuers(),
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            logger.warning("Token de administración inválido | %s", type(exc).__name__)
            raise AdminAuthError(401, "Token inválido") from exc

        if str(claims.get("tid", "")).lower() != str(self._tenant_id).lower():
            raise AdminAuthError(401, "Token de otro tenant")

        roles = tuple(claims.get("roles") or ())
        if self._required_role not in roles:
            raise AdminAuthError(403, f"Se requiere el rol {self._required_role}")

        return AdminPrincipal(
            object_id=str(claims.get("oid", "")),
            name=str(claims.get("preferred_username") or claims.get("upn") or claims.get("name") or ""),
            roles=roles,
        )

    async def validate_async(self, authorization: str | None) -> AdminPrincipal:
        # PyJWKClient hace E/S bloqueante al refrescar claves.
        return await asyncio.to_thread(self.validate, authorization)
