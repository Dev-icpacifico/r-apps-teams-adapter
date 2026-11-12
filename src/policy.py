"""Evaluación de acceso por grupos de Entra ID."""

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str
    matched_groups: tuple[str, ...] = ()


def evaluate_access(
    access_mode: str,
    required_groups: Iterable[str],
    member_groups: Iterable[str],
) -> AccessDecision:
    """Decide si el usuario cumple los grupos requeridos.

    - any: pertenece al menos a uno.
    - all: pertenece a todos.
    Sin grupos requeridos se deniega (no hay áreas abiertas por omisión).
    """
    required = {g.lower() for g in required_groups}
    member = {g.lower() for g in member_groups}

    if not required:
        return AccessDecision(False, "área sin grupos requeridos")

    matched = tuple(sorted(required & member))

    if access_mode == "any":
        if matched:
            return AccessDecision(True, "cumple al menos un grupo", matched)
        return AccessDecision(False, "no pertenece a ningún grupo requerido")

    if access_mode == "all":
        if len(matched) == len(required):
            return AccessDecision(True, "cumple todos los grupos", matched)
        return AccessDecision(
            False,
            f"cumple {len(matched)} de {len(required)} grupos requeridos",
            matched,
        )

    return AccessDecision(False, f"modo de acceso desconocido: {access_mode!r}")


def evaluate_agent_access(area, agent, member_groups: Iterable[str]) -> AccessDecision:
    """Aplica la regla del área (si existe) y la del agente (si existe).

    Se exigen ambas. Un agente sin regla propia en un área sin regla queda
    denegado: no hay agentes abiertos por omisión.
    """
    member = list(member_groups)
    area_decision = None
    if area.required_groups:
        area_decision = evaluate_access(area.access_mode, area.required_groups, member)
        if not area_decision.allowed:
            return AccessDecision(False, f"área: {area_decision.reason}")
    if agent.required_groups:
        decision = evaluate_access(agent.access_mode, agent.required_groups, member)
        if not decision.allowed:
            return AccessDecision(False, f"agente {agent.alias}: {decision.reason}")
        return decision
    if area_decision is not None:
        return area_decision
    return AccessDecision(False, f"agente {agent.alias} sin grupos requeridos")
