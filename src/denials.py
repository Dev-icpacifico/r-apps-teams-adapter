"""Mensajes de rechazo comprensibles.

Cada rechazo debe decir a quien lo recibe (la persona en Teams o el agente
que lo transmite desde ChatGPT) qué requisito falta y cómo pedirlo. Aquí
se calculan las reglas de grupos no cumplidas y se redactan esos textos;
los nombres visibles de los grupos los resuelve AreaService con Graph.
"""

from typing import Iterable

HOW_TO_REQUEST = (
    "Para solicitarlo, escribe «solicitar» mencionando al bot en un canal de tu equipo "
    "y elige «Dar acceso», o pide a un administrador de Agentes ICP que te agregue."
)
RETRY_LOOKUP = (
    "No pude verificar tus permisos en Entra en este momento. Intenta de nuevo en unos "
    "minutos; si se repite, avisa a un administrador de Agentes ICP."
)
HOW_TO_ENABLE_CHANNEL = (
    "Para habilitarlo, escribe «solicitar» mencionando al bot en este canal y elige "
    "«Agregar canales»."
)
ASK_ADMIN = "Pide a un administrador de Agentes ICP que lo habilite."

# Regla no cumplida: (modo, grupos que faltan). En modo «any» se listan
# todos los grupos de la regla (basta uno); en «all», solo los que faltan.
Unmet = tuple[str, tuple[str, ...]]


def unmet_rule(access_mode: str, required: Iterable[str], member: Iterable[str]) -> Unmet | None:
    required_set = {g.lower() for g in required}
    if not required_set:
        return None
    member_set = {g.lower() for g in member}
    if access_mode == "all":
        missing = tuple(sorted(required_set - member_set))
        return ("all", missing) if missing else None
    return None if required_set & member_set else ("any", tuple(sorted(required_set)))


def unmet_for_agent(area, agent, member: Iterable[str]) -> list[Unmet]:
    """Reglas no cumplidas para usar ``agent`` en ``area`` (área y agente)."""
    member = list(member)
    rules = []
    if area is not None and area.required_groups:
        rules.append(unmet_rule(area.access_mode, area.required_groups, member))
    if agent is not None and agent.required_groups:
        rules.append(unmet_rule(agent.access_mode, agent.required_groups, member))
    return [r for r in rules if r is not None]


def group_ids(unmet: Iterable[Unmet]) -> list[str]:
    return list(dict.fromkeys(g for _, groups in unmet for g in groups))


def describe_requirements(unmet: Iterable[Unmet], names: dict[str, str]) -> str:
    """«pertenecer al grupo de Entra «X»» (y variantes), para insertar tras
    «necesitas»."""
    parts = []
    for mode, groups in unmet:
        labels = [f"«{names.get(g, g)}»" for g in groups]
        if len(labels) == 1:
            parts.append(f"pertenecer al grupo de Entra {labels[0]}")
        elif mode == "any":
            parts.append("pertenecer a uno de estos grupos de Entra: " + _join(labels, "o"))
        else:
            parts.append("pertenecer a los grupos de Entra " + _join(labels, "y"))
    return " y además ".join(parts) if parts else "un permiso adicional"


def _join(items: list[str], word: str) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" {word} " + items[-1]
