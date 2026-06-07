"""Inquilinos (marcas) del motor de marketing — base multi-marca del Autopilot.

Hoy el motor estaba cableado SOLO al CMC (cuenta Meta, márgenes de salud, atribución
por el BI del CMC). Esta capa abstrae "qué marca está operando" para que el mismo motor
sirva a varias marcas del holding (CMC, Meulen, …) — el paso #3 (cross-marca).

Diseño backward-compatible: el inquilino activo por defecto es CMC y resuelve los mismos
valores que antes (misma cuenta Meta, mismo dueño), así que el comportamiento de prod es
idéntico. Las otras marcas quedan registradas pero DESACTIVADAS hasta tener su cuenta de
ads + su fuente de atribución.

El inquilino activo se elige con la env `AUTOPILOT_TENANT` (default "cmc"). Operar varias
marcas EN PARALELO (no solo cambiar la activa) es el siguiente paso: pasar el `Tenant` por
parámetro a world_state/policy/cac_local. Por ahora: un activo a la vez, switchable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    id: str                  # slug corto: "cmc", "meulen"
    label: str               # nombre legible
    ad_account: str          # cuenta Meta Ads (act_…); "" si aún no tiene
    currency: str            # "CLP"
    margin_profile: str      # "salud" | "retail" — qué modelo de margen usa policy
    attribution: str         # "bi" (BI propio) | "none" (sin fuente aún)
    owner_phone: str         # a quién se le avisan las propuestas
    enabled: bool            # False = registrada pero el motor no opera esta marca


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = (os.getenv(n) or "").strip()
        if v:
            return v
    return default


# Registro de inquilinos. CMC = el de hoy (mismos defaults que el motor original).
# Meulen = stub apagado: cuando tenga cuenta de ads + atribución, se prende.
def _registry() -> dict[str, Tenant]:
    return {
        "cmc": Tenant(
            id="cmc",
            label="Centro Médico Carampangue",
            # Mismo default que meta_ads original → prod idéntico.
            ad_account=_env("META_AD_ACCOUNT_ID", default="act_220608142267129"),
            currency="CLP",
            margin_profile="salud",
            attribution="bi",
            owner_phone=_env("AUTOPILOT_OWNER_PHONE", "ADMIN_ALERT_PHONE"),
            enabled=True,
        ),
        "meulen": Tenant(
            id="meulen",
            label="Supermercado Meulen",
            ad_account=_env("MEULEN_AD_ACCOUNT_ID"),   # vacío hasta tener cuenta
            currency="CLP",
            margin_profile="retail",
            attribution="none",                        # sin fuente de atribución aún
            owner_phone=_env("MEULEN_OWNER_PHONE", "AUTOPILOT_OWNER_PHONE"),
            enabled=False,                             # registrado, no operativo
        ),
    }


def all_tenants() -> dict[str, Tenant]:
    return _registry()


def get(tenant_id: str) -> Tenant:
    reg = _registry()
    if tenant_id not in reg:
        raise KeyError(f"tenant desconocido: {tenant_id}")
    return reg[tenant_id]


def active() -> Tenant:
    """Inquilino que está operando ahora. Default CMC (comportamiento histórico)."""
    tid = (os.getenv("AUTOPILOT_TENANT") or "cmc").strip().lower()
    reg = _registry()
    return reg.get(tid, reg["cmc"])
