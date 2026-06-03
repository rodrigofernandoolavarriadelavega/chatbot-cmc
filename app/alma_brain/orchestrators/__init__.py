"""Registro de orquestadores de Alma.

Cada módulo concreto expone una instancia `ORQ`. Acá las recolectamos en
`REGISTRY` y ofrecemos runners. Importar un módulo roto NO tumba el registro
(se loggea y se sigue) — degradación elegante hasta a nivel de catálogo.
"""
import importlib
import logging

from .base import Orchestrator, Proposal, OrqResult  # noqa: F401

log = logging.getLogger("bot")

# Orden = orden de aparición en el panel. Agregar acá al crear uno nuevo.
_MODULES = [
    "confirmaciones",
    "hueco_proactivo",
    "no_show_recovery",
    "adherencia_kine",
    "controles_ortodoncia",
    "cobranza_ortodoncia",
    "demanda_abrir_agenda",
    "inventario_compra",
    "reactivacion",
    "reputacion_nps",
    "resultados_examenes",
    "preventivo_edad",
    "cumpleanos",
    "crosssell_postconsulta",
    "campanas_estacionales",
    "agenda_salud",
    "ges_backlog",
    "primera_vez_sin_retorno",
    "control_cronico",
    "ficha_incompleta",
    "ads_anomalia",
    "referral_sin_cerrar",
    "conversacion_parada",
]

REGISTRY: dict[str, Orchestrator] = {}

for _m in _MODULES:
    try:
        _mod = importlib.import_module(f".{_m}", __package__)
        _orq = getattr(_mod, "ORQ", None)
        if _orq is not None:
            REGISTRY[_orq.name] = _orq
        else:
            log.warning("orq: módulo %s no expone ORQ", _m)
    except Exception as e:  # noqa: BLE001
        log.warning("orq: no pude cargar módulo %s: %s", _m, e)


def catalog() -> list[dict]:
    """Metadatos de todos los orquestadores (para el panel)."""
    return [orq.meta() for orq in REGISTRY.values()]


async def run_one(name: str, *, mode: str = "dry") -> dict:
    orq = REGISTRY.get(name)
    if orq is None:
        return {"error": f"orquestador desconocido: {name}"}
    res = await orq.run(mode=mode)
    return res.as_dict()


async def run_all(*, mode: str = "dry") -> list[dict]:
    """Corre todos. En 'dry' es un barrido read-only seguro (preview del panel).
    En 'propose'/'execute' cada orquestador respeta su propio flag."""
    out = []
    for orq in REGISTRY.values():
        try:
            res = await orq.run(mode=mode)
            out.append(res.as_dict())
        except Exception as e:  # noqa: BLE001
            log.warning("orq %s: run() falló: %s", orq.name, e)
            out.append({"name": orq.name, "error": str(e)})
    return out
