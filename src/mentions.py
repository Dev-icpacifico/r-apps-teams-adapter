"""Nombre con el que los usuarios mencionan al bot en Teams.

``activity.recipient.name`` es el nombre del registro del bot en Azure
(p. ej. «bot-teams-adapter-dev»), que no sirve para escribir una mención.
El nombre visible de la app es el que viene en la entidad de mención con
que el usuario llamó al bot (``<at>Agentes ICP DEV</at>``).
"""

import re

_AT_TAGS = re.compile(r"</?at>", re.IGNORECASE)


def _get(obj, name):
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def bot_display_name(activity, fallback: str = "") -> str:
    """Nombre visible del bot según la mención; si no hay, ``fallback`` y,
    por último, el nombre del registro."""
    recipient = _get(activity, "recipient")
    bot_id = _get(recipient, "id")
    for entity in _get(activity, "entities") or []:
        if _get(entity, "type") != "mention":
            continue
        mentioned = _get(entity, "mentioned")
        if not bot_id or _get(mentioned, "id") != bot_id:
            continue
        text = _AT_TAGS.sub("", _get(entity, "text") or "").strip()
        name = text or (_get(mentioned, "name") or "").strip()
        if name:
            return name
    return fallback or (_get(recipient, "name") or "")
