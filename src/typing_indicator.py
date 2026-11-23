"""Indicador «escribiendo» en Teams.

Es solo cortesía visual: si Teams no responde al enviarlo (p. ej. un corte
de conexión TLS con smba.trafficmanager.net, visto en producción el
23/11/2026), el mensaje del usuario se debe procesar igual. Antes la
excepción abortaba el manejador y el mensaje se perdía sin aviso.
"""

import logging

from microsoft_teams.api import TypingActivityInput

logger = logging.getLogger("teams-adapter")


async def send_typing(ctx) -> bool:
    """Envía el indicador. Devuelve False si falló (ya registrado)."""
    try:
        await ctx.reply(TypingActivityInput())
        return True
    except Exception as exc:  # noqa: BLE001 - no debe interrumpir el mensaje
        logger.warning(
            "No se pudo enviar el indicador de escritura; se continúa | %s",
            type(exc).__name__,
        )
        return False
