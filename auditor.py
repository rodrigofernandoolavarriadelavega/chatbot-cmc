#!/usr/bin/env python3
"""
Auditor de Conciliación Financiera — Centro Médico Carampangue
==============================================================
Lee archivos CSV de cada fuente de pago y genera un informe de conciliación.

Estructura de carpetas esperada:
  RECEPCION/         → Google Sheets exportado como CSV
  MEDILINK/          → Export de Medilink (pagos/atenciones)
  TRANSFERENCIA/     → Extracto Itaú (CSV banco)
  EFECTIVO/          → Extracto BancoEstado (depósitos en efectivo)
  TRANSBANK_DEBITO/  → Reporte Transbank débito
  TRANSBANK_CREDITO/ → Reporte Transbank crédito

Uso:
  python auditor.py                    # audita todos los archivos disponibles
  python auditor.py --desde 2026-03-01 --hasta 2026-03-31   # filtra por fecha
  python auditor.py --output informe.html  # exporta informe HTML
"""

import os
import sys
import re
import argparse
import csv
import json
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
import unicodedata

# ─────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent

FOLDERS = {
    "recepcion":   BASE_DIR / "RECEPCION",
    "medilink":    BASE_DIR / "MEDILINK",
    "transferencia": BASE_DIR / "TRANSFERENCIA",
    "efectivo":    BASE_DIR / "EFECTIVO",
    "transbank_debito":  BASE_DIR / "TRANSBANK_DEBITO",
    "transbank_credito": BASE_DIR / "TRANSBANK_CREDITO",
}

MEDIO_PAGO_MAP = {
    "transferencia": "TRANSFERENCIA",
    "transfer":      "TRANSFERENCIA",
    "transf":        "TRANSFERENCIA",
    "trf":           "TRANSFERENCIA",
    "efectivo":      "EFECTIVO",
    "cash":          "EFECTIVO",
    "efvo":          "EFECTIVO",
    "debito":        "TRANSBANK_DEBITO",
    "débito":        "TRANSBANK_DEBITO",
    "db":            "TRANSBANK_DEBITO",
    "transbank debito": "TRANSBANK_DEBITO",
    "transbank débito": "TRANSBANK_DEBITO",
    "credito":       "TRANSBANK_CREDITO",
    "crédito":       "TRANSBANK_CREDITO",
    "cr":            "TRANSBANK_CREDITO",
    "transbank credito":  "TRANSBANK_CREDITO",
    "transbank crédito":  "TRANSBANK_CREDITO",
    "transbank":     "TRANSBANK_DEBITO",  # si no especifica tipo, asume débito
}

TIPOS_HALLAZGO = [
    "FALTANTE",
    "SOBRANTE",
    "DIFERENCIA_MONTO",
    "DIFERENCIA_FECHA",
    "DUPLICADO",
    "MEDIO_PAGO_INCORRECTO",
    "SIN_RESPALDO_BANCARIO",
    "SIN_RESPALDO_INTERNO",
    "REQUIERE_REVISION_MANUAL",
]

TOLERANCIA_DIAS = 3      # días de diferencia aceptables al buscar match bancario
TOLERANCIA_MONTO = 1.0   # diferencia máxima en pesos para considerar coincidencia exacta


# ─────────────────────────────────────────────────────────────
# Estructuras de datos
# ─────────────────────────────────────────────────────────────

@dataclass
class Pago:
    fuente: str
    fecha: Optional[date]
    paciente: str
    monto: float
    medio: str          # TRANSFERENCIA | EFECTIVO | TRANSBANK_DEBITO | TRANSBANK_CREDITO
    referencia: str = ""
    profesional: str = ""
    observacion: str = ""
    conciliado: bool = False
    id: int = 0


@dataclass
class MovimientoBancario:
    fuente: str         # TRANSFERENCIA | EFECTIVO | TRANSBANK_DEBITO | TRANSBANK_CREDITO
    fecha: Optional[date]
    monto: float
    descripcion: str = ""
    referencia: str = ""
    tipo: str = "CREDITO"   # CREDITO | DEBITO
    conciliado: bool = False
    id: int = 0


@dataclass
class Hallazgo:
    fecha: Optional[date]
    paciente: str
    monto_interno: Optional[float]
    monto_externo: Optional[float]
    fuente_interna: str
    fuente_externa: str
    medio: str
    tipo: str
    comentario: str
    prioridad: str = "MEDIA"    # ALTA | MEDIA | BAJA


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def normalizar_texto(s: str) -> str:
    """Minúsculas, sin tildes, sin puntuación extraña."""
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s


def normalizar_medio(s: str) -> str:
    key = normalizar_texto(s)
    return MEDIO_PAGO_MAP.get(key, key.upper())


def parsear_monto(s: str) -> Optional[float]:
    """Convierte '$1.234,56' o '1234.56' a float."""
    if not s:
        return None
    s = re.sub(r"[^\d,\.]", "", s.strip())
    # Formato chileno: punto como separador de miles, coma como decimal
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s and "." not in s:
        s = s.replace(",", ".")
    elif "." in s and s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


FORMATOS_FECHA = [
    "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y",
    "%d-%m-%y", "%Y/%m/%d", "%d.%m.%Y",
]


def parsear_fecha(s: str) -> Optional[date]:
    s = s.strip()
    for fmt in FORMATOS_FECHA:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def similitud_nombre(a: str, b: str) -> float:
    """Similitud simple entre dos nombres (0.0 a 1.0)."""
    a = normalizar_texto(a)
    b = normalizar_texto(b)
    if a == b:
        return 1.0
    tokens_a = set(a.split())
    tokens_b = set(b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    interseccion = tokens_a & tokens_b
    return len(interseccion) / max(len(tokens_a), len(tokens_b))


def leer_csvs(carpeta: Path) -> list[dict]:
    """Lee todos los CSV de una carpeta y retorna lista de dicts."""
    filas = []
    if not carpeta.exists():
        return filas
    for archivo in sorted(carpeta.glob("*.csv")):
        try:
            with open(archivo, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row["_archivo"] = archivo.name
                    filas.append(row)
        except Exception as e:
            print(f"  [WARN] No se pudo leer {archivo.name}: {e}", file=sys.stderr)
    return filas


# ─────────────────────────────────────────────────────────────
# Parsers por fuente
# ─────────────────────────────────────────────────────────────

def _col(row: dict, *opciones) -> str:
    """Busca la primera columna existente (insensible a mayúsculas/tildes)."""
    norm_row = {normalizar_texto(k): v for k, v in row.items()}
    for nombre in opciones:
        clave = normalizar_texto(nombre)
        if clave in norm_row:
            return norm_row[clave] or ""
    return ""


def parse_recepcion(filas: list[dict]) -> list[Pago]:
    """
    Columnas esperadas (flexibles):
      fecha | paciente/nombre | monto/valor | medio/forma_pago | profesional | observacion
    """
    pagos = []
    for i, row in enumerate(filas):
        fecha  = parsear_fecha(_col(row, "fecha", "date"))
        paciente = _col(row, "paciente", "nombre", "nombre_paciente", "patient")
        monto_raw = _col(row, "monto", "valor", "total", "amount")
        medio_raw = _col(row, "medio", "forma_pago", "medio_pago", "pago", "payment")
        profesional = _col(row, "profesional", "medico", "doctor")
        obs = _col(row, "observacion", "obs", "comentario", "nota")

        monto = parsear_monto(monto_raw)
        if monto is None or monto <= 0:
            continue
        medio = normalizar_medio(medio_raw) if medio_raw else "DESCONOCIDO"

        pagos.append(Pago(
            fuente="RECEPCION",
            fecha=fecha,
            paciente=paciente.strip(),
            monto=monto,
            medio=medio,
            profesional=profesional.strip(),
            observacion=obs.strip(),
            id=i,
        ))
    return pagos


def parse_medilink(filas: list[dict]) -> list[Pago]:
    """
    Columnas esperadas (adaptar según el export de Medilink):
      fecha | paciente | prestacion | monto | estado | profesional
    """
    pagos = []
    for i, row in enumerate(filas):
        fecha    = parsear_fecha(_col(row, "fecha", "fecha_atencion", "date"))
        paciente = _col(row, "paciente", "nombre", "nombre_paciente")
        monto_raw = _col(row, "monto", "valor", "total", "precio", "copago")
        medio_raw = _col(row, "medio", "forma_pago", "pago", "tipo_pago")
        profesional = _col(row, "profesional", "medico", "especialista")

        monto = parsear_monto(monto_raw)
        if monto is None or monto <= 0:
            continue
        medio = normalizar_medio(medio_raw) if medio_raw else "DESCONOCIDO"

        pagos.append(Pago(
            fuente="MEDILINK",
            fecha=fecha,
            paciente=paciente.strip(),
            monto=monto,
            medio=medio,
            profesional=profesional.strip(),
            id=i,
        ))
    return pagos


def parse_banco(filas: list[dict], fuente: str) -> list[MovimientoBancario]:
    """
    Extracto bancario genérico (Itaú / BancoEstado).
    Columnas esperadas:
      fecha | descripcion/glosa | monto/cargo/abono | referencia
    """
    movs = []
    for i, row in enumerate(filas):
        fecha = parsear_fecha(_col(row, "fecha", "date", "fecha_operacion"))
        desc  = _col(row, "descripcion", "glosa", "detalle", "concepto", "description")
        ref   = _col(row, "referencia", "ref", "numero", "n_operacion", "voucher")

        # Itaú/BancoEstado pueden tener columnas separadas cargo/abono
        abono_raw = _col(row, "abono", "credito", "credit", "ingreso", "deposito")
        cargo_raw = _col(row, "cargo", "debito", "debit", "egreso", "retiro")
        monto_raw = _col(row, "monto", "importe", "amount", "valor")

        monto = None
        tipo  = "CREDITO"
        if abono_raw:
            monto = parsear_monto(abono_raw)
            tipo  = "CREDITO"
        elif cargo_raw:
            monto = parsear_monto(cargo_raw)
            tipo  = "DEBITO"
        elif monto_raw:
            monto = parsear_monto(monto_raw)
            tipo  = "CREDITO"

        if monto is None or monto <= 0:
            continue

        movs.append(MovimientoBancario(
            fuente=fuente,
            fecha=fecha,
            monto=monto,
            descripcion=desc.strip(),
            referencia=ref.strip(),
            tipo=tipo,
            id=i,
        ))
    return movs


def parse_transbank(filas: list[dict], fuente: str) -> list[MovimientoBancario]:
    """
    Reporte Transbank (débito o crédito).
    Columnas esperadas:
      fecha | monto | tipo_tarjeta | voucher/n_operacion | estado
    """
    movs = []
    for i, row in enumerate(filas):
        fecha = parsear_fecha(_col(row, "fecha", "fecha_transaccion", "date"))
        monto_raw = _col(row, "monto", "importe", "amount", "total")
        voucher   = _col(row, "voucher", "n_operacion", "codigo", "referencia", "orden")
        desc      = _col(row, "descripcion", "comercio", "glosa", "estado")

        # Filtrar transacciones no aprobadas
        estado = normalizar_texto(_col(row, "estado", "resultado", "status"))
        if estado and estado not in ("aprobado", "aceptado", "approved", "ok", ""):
            continue

        monto = parsear_monto(monto_raw)
        if monto is None or monto <= 0:
            continue

        movs.append(MovimientoBancario(
            fuente=fuente,
            fecha=fecha,
            monto=monto,
            descripcion=desc.strip(),
            referencia=voucher.strip(),
            tipo="CREDITO",
            id=i,
        ))
    return movs


# ─────────────────────────────────────────────────────────────
# Motor de conciliación
# ─────────────────────────────────────────────────────────────

class Auditor:
    def __init__(self, desde: Optional[date] = None, hasta: Optional[date] = None):
        self.desde = desde
        self.hasta = hasta
        self.hallazgos: list[Hallazgo] = []

        self.pagos_recepcion: list[Pago] = []
        self.pagos_medilink:  list[Pago] = []
        self.movs_transferencia: list[MovimientoBancario] = []
        self.movs_efectivo:      list[MovimientoBancario] = []
        self.movs_tb_debito:     list[MovimientoBancario] = []
        self.movs_tb_credito:    list[MovimientoBancario] = []

    def cargar_datos(self):
        print("Cargando datos...")

        r = parse_recepcion(leer_csvs(FOLDERS["recepcion"]))
        m = parse_medilink(leer_csvs(FOLDERS["medilink"]))
        t = parse_banco(leer_csvs(FOLDERS["transferencia"]), "TRANSFERENCIA")
        e = parse_banco(leer_csvs(FOLDERS["efectivo"]),    "EFECTIVO")
        td = parse_transbank(leer_csvs(FOLDERS["transbank_debito"]),  "TRANSBANK_DEBITO")
        tc = parse_transbank(leer_csvs(FOLDERS["transbank_credito"]), "TRANSBANK_CREDITO")

        def filtrar_pagos(lst):
            return [p for p in lst if self._en_rango(p.fecha)]

        def filtrar_movs(lst):
            return [m for m in lst if self._en_rango(m.fecha)]

        self.pagos_recepcion = filtrar_pagos(r)
        self.pagos_medilink  = filtrar_pagos(m)
        self.movs_transferencia = filtrar_movs(t)
        self.movs_efectivo      = filtrar_movs(e)
        self.movs_tb_debito     = filtrar_movs(td)
        self.movs_tb_credito    = filtrar_movs(tc)

        print(f"  Recepción:          {len(self.pagos_recepcion)} registros")
        print(f"  Medilink:           {len(self.pagos_medilink)} registros")
        print(f"  Transferencias:     {len(self.movs_transferencia)} movimientos (Itaú)")
        print(f"  Efectivo:           {len(self.movs_efectivo)} movimientos (BancoEstado)")
        print(f"  Transbank Débito:   {len(self.movs_tb_debito)} transacciones")
        print(f"  Transbank Crédito:  {len(self.movs_tb_credito)} transacciones")

    def _en_rango(self, d: Optional[date]) -> bool:
        if d is None:
            return True  # sin fecha → incluir siempre, marcar después
        if self.desde and d < self.desde:
            return False
        if self.hasta and d > self.hasta:
            return False
        return True

    # ── Cruce 1: Recepción vs Medilink ────────────────────────

    def cruzar_recepcion_medilink(self):
        """Detecta pagos que están en uno pero no en el otro, y diferencias de monto."""
        usados_med = set()

        for rec in self.pagos_recepcion:
            mejor_match = None
            mejor_score = 0.0

            for j, med in enumerate(self.pagos_medilink):
                if j in usados_med:
                    continue
                if rec.fecha and med.fecha:
                    delta = abs((rec.fecha - med.fecha).days)
                    if delta > 1:
                        continue
                sim = similitud_nombre(rec.paciente, med.paciente)
                if sim < 0.4:
                    continue
                score = sim + (0.3 if abs(rec.monto - med.monto) < TOLERANCIA_MONTO else 0)
                if score > mejor_score:
                    mejor_score = score
                    mejor_match = j

            if mejor_match is not None and mejor_score >= 0.6:
                med = self.pagos_medilink[mejor_match]
                usados_med.add(mejor_match)
                rec.conciliado = True
                med.conciliado = True

                # Verificar diferencias aunque haya match
                if abs(rec.monto - med.monto) > TOLERANCIA_MONTO:
                    self._agregar(Hallazgo(
                        fecha=rec.fecha,
                        paciente=rec.paciente,
                        monto_interno=rec.monto,
                        monto_externo=med.monto,
                        fuente_interna="RECEPCION",
                        fuente_externa="MEDILINK",
                        medio=rec.medio,
                        tipo="DIFERENCIA_MONTO",
                        comentario=f"Recepción ${rec.monto:,.0f} vs Medilink ${med.monto:,.0f}",
                        prioridad="ALTA",
                    ))
                if rec.medio != med.medio and med.medio != "DESCONOCIDO" and rec.medio != "DESCONOCIDO":
                    self._agregar(Hallazgo(
                        fecha=rec.fecha,
                        paciente=rec.paciente,
                        monto_interno=rec.monto,
                        monto_externo=med.monto,
                        fuente_interna="RECEPCION",
                        fuente_externa="MEDILINK",
                        medio=rec.medio,
                        tipo="MEDIO_PAGO_INCORRECTO",
                        comentario=f"Recepción dice {rec.medio}, Medilink dice {med.medio}",
                        prioridad="MEDIA",
                    ))
            else:
                # No se encontró match en Medilink
                self._agregar(Hallazgo(
                    fecha=rec.fecha,
                    paciente=rec.paciente,
                    monto_interno=rec.monto,
                    monto_externo=None,
                    fuente_interna="RECEPCION",
                    fuente_externa="MEDILINK",
                    medio=rec.medio,
                    tipo="FALTANTE",
                    comentario="Pago en recepción sin registro equivalente en Medilink",
                    prioridad="ALTA",
                ))

        # Pagos Medilink sin match en recepción
        for j, med in enumerate(self.pagos_medilink):
            if j not in usados_med:
                self._agregar(Hallazgo(
                    fecha=med.fecha,
                    paciente=med.paciente,
                    monto_interno=None,
                    monto_externo=med.monto,
                    fuente_interna="RECEPCION",
                    fuente_externa="MEDILINK",
                    medio=med.medio,
                    tipo="SOBRANTE",
                    comentario="Pago en Medilink sin registro equivalente en recepción",
                    prioridad="ALTA",
                ))

    # ── Cruce 2: Transferencias vs Itaú ──────────────────────

    def cruzar_transferencias(self):
        pagos_transf = [p for p in self.pagos_recepcion if p.medio == "TRANSFERENCIA"]
        movs = list(self.movs_transferencia)
        usados = set()

        for pago in pagos_transf:
            match_idx = self._buscar_mov_bancario(pago, movs, usados)
            if match_idx is not None:
                mov = movs[match_idx]
                usados.add(match_idx)
                pago.conciliado = True
                mov.conciliado = True
                if abs(pago.monto - mov.monto) > TOLERANCIA_MONTO:
                    self._agregar(Hallazgo(
                        fecha=pago.fecha,
                        paciente=pago.paciente,
                        monto_interno=pago.monto,
                        monto_externo=mov.monto,
                        fuente_interna="RECEPCION",
                        fuente_externa="ITAU",
                        medio="TRANSFERENCIA",
                        tipo="DIFERENCIA_MONTO",
                        comentario=f"Recepción ${pago.monto:,.0f} vs Itaú ${mov.monto:,.0f}",
                        prioridad="ALTA",
                    ))
            else:
                self._agregar(Hallazgo(
                    fecha=pago.fecha,
                    paciente=pago.paciente,
                    monto_interno=pago.monto,
                    monto_externo=None,
                    fuente_interna="RECEPCION",
                    fuente_externa="ITAU",
                    medio="TRANSFERENCIA",
                    tipo="SIN_RESPALDO_BANCARIO",
                    comentario=f"Transferencia ${pago.monto:,.0f} no encontrada en extracto Itaú",
                    prioridad="ALTA",
                ))

        for i, mov in enumerate(movs):
            if i not in usados and mov.tipo == "CREDITO":
                self._agregar(Hallazgo(
                    fecha=mov.fecha,
                    paciente="—",
                    monto_interno=None,
                    monto_externo=mov.monto,
                    fuente_interna="RECEPCION",
                    fuente_externa="ITAU",
                    medio="TRANSFERENCIA",
                    tipo="SIN_RESPALDO_INTERNO",
                    comentario=f"Abono ${mov.monto:,.0f} en Itaú sin registro en recepción. Glosa: {mov.descripcion or '—'}",
                    prioridad="MEDIA",
                ))

    # ── Cruce 3: Efectivo vs BancoEstado ─────────────────────

    def cruzar_efectivo(self):
        """
        Suma el efectivo registrado por día y lo compara contra depósitos en BancoEstado.
        Los depósitos pueden agrupar varios días.
        """
        # Agrupar efectivo por fecha
        efvo_por_dia: dict[Optional[date], float] = defaultdict(float)
        for p in self.pagos_recepcion:
            if p.medio == "EFECTIVO":
                efvo_por_dia[p.fecha] += p.monto

        if not efvo_por_dia:
            return

        total_efectivo = sum(efvo_por_dia.values())
        total_depositado = sum(m.monto for m in self.movs_efectivo if m.tipo == "CREDITO")

        diferencia = total_efectivo - total_depositado

        # Intentar conciliación exacta por fecha
        depositos = [m for m in self.movs_efectivo if m.tipo == "CREDITO"]

        for fec, total_dia in sorted(efvo_por_dia.items()):
            # Buscar depósito del mismo día o hasta TOLERANCIA_DIAS días después
            match = None
            for dep in depositos:
                if dep.conciliado:
                    continue
                if dep.fecha is None or fec is None:
                    continue
                delta = (dep.fecha - fec).days
                if 0 <= delta <= TOLERANCIA_DIAS and abs(dep.monto - total_dia) <= TOLERANCIA_MONTO:
                    match = dep
                    break

            if match:
                match.conciliado = True
            else:
                self._agregar(Hallazgo(
                    fecha=fec,
                    paciente="—",
                    monto_interno=total_dia,
                    monto_externo=None,
                    fuente_interna="RECEPCION",
                    fuente_externa="BANCO_ESTADO",
                    medio="EFECTIVO",
                    tipo="SIN_RESPALDO_BANCARIO",
                    comentario=(
                        f"Efectivo del día ${total_dia:,.0f} sin depósito equivalente en BancoEstado "
                        f"(±{TOLERANCIA_DIAS} días)"
                    ),
                    prioridad="ALTA" if total_dia > 50000 else "MEDIA",
                ))

        # Depósitos sin respaldo
        for dep in depositos:
            if not dep.conciliado:
                self._agregar(Hallazgo(
                    fecha=dep.fecha,
                    paciente="—",
                    monto_interno=None,
                    monto_externo=dep.monto,
                    fuente_interna="RECEPCION",
                    fuente_externa="BANCO_ESTADO",
                    medio="EFECTIVO",
                    tipo="REQUIERE_REVISION_MANUAL",
                    comentario=(
                        f"Depósito ${dep.monto:,.0f} en BancoEstado no se pudo conciliar "
                        f"automáticamente con efectivo del día. Puede agrupar varios días."
                    ),
                    prioridad="MEDIA",
                ))

        # Resumen global efectivo
        if abs(diferencia) > TOLERANCIA_MONTO:
            signo = "menos" if diferencia > 0 else "más"
            self._agregar(Hallazgo(
                fecha=None,
                paciente="RESUMEN EFECTIVO",
                monto_interno=total_efectivo,
                monto_externo=total_depositado,
                fuente_interna="RECEPCION",
                fuente_externa="BANCO_ESTADO",
                medio="EFECTIVO",
                tipo="DIFERENCIA_MONTO",
                comentario=(
                    f"Total efectivo registrado ${total_efectivo:,.0f} vs "
                    f"total depositado ${total_depositado:,.0f} → "
                    f"diferencia de ${abs(diferencia):,.0f} ({signo} depositado)"
                ),
                prioridad="ALTA",
            ))

    # ── Cruce 4: Transbank interno vs Plataforma ──────────────

    def cruzar_transbank(self, medio: str, movs_plataforma: list[MovimientoBancario]):
        pagos = [p for p in self.pagos_recepcion if p.medio == medio]
        usados = set()

        for pago in pagos:
            match_idx = self._buscar_mov_bancario(pago, movs_plataforma, usados)
            if match_idx is not None:
                mov = movs_plataforma[match_idx]
                usados.add(match_idx)
                pago.conciliado = True
                mov.conciliado = True
                if abs(pago.monto - mov.monto) > TOLERANCIA_MONTO:
                    self._agregar(Hallazgo(
                        fecha=pago.fecha,
                        paciente=pago.paciente,
                        monto_interno=pago.monto,
                        monto_externo=mov.monto,
                        fuente_interna="RECEPCION",
                        fuente_externa=medio,
                        medio=medio,
                        tipo="DIFERENCIA_MONTO",
                        comentario=f"${pago.monto:,.0f} en recepción vs ${mov.monto:,.0f} en Transbank",
                        prioridad="ALTA",
                    ))
            else:
                self._agregar(Hallazgo(
                    fecha=pago.fecha,
                    paciente=pago.paciente,
                    monto_interno=pago.monto,
                    monto_externo=None,
                    fuente_interna="RECEPCION",
                    fuente_externa=medio,
                    medio=medio,
                    tipo="SIN_RESPALDO_BANCARIO",
                    comentario=f"Pago {medio} ${pago.monto:,.0f} no encontrado en plataforma Transbank",
                    prioridad="ALTA",
                ))

        for i, mov in enumerate(movs_plataforma):
            if i not in usados:
                self._agregar(Hallazgo(
                    fecha=mov.fecha,
                    paciente="—",
                    monto_interno=None,
                    monto_externo=mov.monto,
                    fuente_interna="RECEPCION",
                    fuente_externa=medio,
                    medio=medio,
                    tipo="SIN_RESPALDO_INTERNO",
                    comentario=f"Transacción ${mov.monto:,.0f} en Transbank sin registro en recepción",
                    prioridad="MEDIA",
                ))

    # ── Helpers internos ──────────────────────────────────────

    def _buscar_mov_bancario(
        self,
        pago: Pago,
        movs: list[MovimientoBancario],
        usados: set,
        tolerancia_monto: float = TOLERANCIA_MONTO,
        tolerancia_dias: int = TOLERANCIA_DIAS,
    ) -> Optional[int]:
        mejor_idx = None
        mejor_delta = float("inf")

        for i, mov in enumerate(movs):
            if i in usados:
                continue
            if abs(pago.monto - mov.monto) > tolerancia_monto:
                continue
            if pago.fecha and mov.fecha:
                delta = abs((pago.fecha - mov.fecha).days)
                if delta > tolerancia_dias:
                    continue
                if delta < mejor_delta:
                    mejor_delta = delta
                    mejor_idx = i
            elif pago.fecha is None or mov.fecha is None:
                # Sin fecha, aceptar por monto
                if mejor_idx is None:
                    mejor_idx = i

        return mejor_idx

    def _agregar(self, h: Hallazgo):
        self.hallazgos.append(h)

    # ── Ejecutar auditoría completa ───────────────────────────

    def auditar(self):
        self.cargar_datos()
        print("\nEjecutando conciliaciones...")
        self.cruzar_recepcion_medilink()
        self.cruzar_transferencias()
        self.cruzar_efectivo()
        self.cruzar_transbank("TRANSBANK_DEBITO",  self.movs_tb_debito)
        self.cruzar_transbank("TRANSBANK_CREDITO", self.movs_tb_credito)
        print(f"  {len(self.hallazgos)} hallazgos detectados.")

    # ─────────────────────────────────────────────────────────
    # Cálculos de resumen
    # ─────────────────────────────────────────────────────────

    def _totales_internos(self) -> dict:
        totales = defaultdict(float)
        for p in self.pagos_recepcion:
            totales[p.medio] += p.monto
        return dict(totales)

    def _totales_externos(self) -> dict:
        return {
            "TRANSFERENCIA":    sum(m.monto for m in self.movs_transferencia if m.tipo == "CREDITO"),
            "EFECTIVO":         sum(m.monto for m in self.movs_efectivo if m.tipo == "CREDITO"),
            "TRANSBANK_DEBITO": sum(m.monto for m in self.movs_tb_debito),
            "TRANSBANK_CREDITO":sum(m.monto for m in self.movs_tb_credito),
        }

    def _cuadre_diario(self) -> list[dict]:
        fechas: set[Optional[date]] = set()
        for p in self.pagos_recepcion:
            fechas.add(p.fecha)
        for p in self.pagos_medilink:
            fechas.add(p.fecha)

        filas = []
        for fec in sorted(f for f in fechas if f is not None):
            rec   = sum(p.monto for p in self.pagos_recepcion if p.fecha == fec)
            med   = sum(p.monto for p in self.pagos_medilink  if p.fecha == fec)
            transf = sum(m.monto for m in self.movs_transferencia if m.fecha == fec and m.tipo == "CREDITO")
            efvo  = sum(m.monto for m in self.movs_efectivo    if m.fecha == fec and m.tipo == "CREDITO")
            tbd   = sum(m.monto for m in self.movs_tb_debito   if m.fecha == fec)
            tbc   = sum(m.monto for m in self.movs_tb_credito  if m.fecha == fec)

            dif = rec - (transf + efvo + tbd + tbc)

            if abs(dif) < TOLERANCIA_MONTO:
                estado = "CUADRA"
            elif abs(dif) < rec * 0.05:
                estado = "CUADRA CON OBSERVACIONES"
            else:
                estado = "NO CUADRA"

            filas.append({
                "fecha": fec.strftime("%d/%m/%Y"),
                "recepcion": rec,
                "medilink": med,
                "itau": transf,
                "banco_estado": efvo,
                "transbank": tbd + tbc,
                "diferencia": dif,
                "estado": estado,
            })
        return filas

    # ─────────────────────────────────────────────────────────
    # Reportes
    # ─────────────────────────────────────────────────────────

    def imprimir_informe(self):
        sep = "─" * 80
        totales_int = self._totales_internos()
        totales_ext = self._totales_externos()
        cuadre = self._cuadre_diario()

        total_int = sum(totales_int.values())
        total_ext = sum(totales_ext.values())
        n_ok = sum(1 for c in cuadre if c["estado"] == "CUADRA")
        n_obs = sum(1 for c in cuadre if c["estado"] == "CUADRA CON OBSERVACIONES")
        n_no  = sum(1 for c in cuadre if c["estado"] == "NO CUADRA")
        n_hall_alta = sum(1 for h in self.hallazgos if h.prioridad == "ALTA")

        nivel_riesgo = "BAJO"
        if n_hall_alta > 5 or n_no > 0:
            nivel_riesgo = "ALTO"
        elif n_hall_alta > 0 or n_obs > 0:
            nivel_riesgo = "MEDIO"

        periodo = ""
        if self.desde or self.hasta:
            periodo = f"{self.desde or '...'} → {self.hasta or '...'}"

        print()
        print("=" * 80)
        print("  INFORME DE CONCILIACIÓN FINANCIERA — CENTRO MÉDICO CARAMPANGUE")
        if periodo:
            print(f"  Período: {periodo}")
        print(f"  Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
        print("=" * 80)

        # A. Resumen ejecutivo
        print()
        print("A. RESUMEN EJECUTIVO")
        print(sep)
        print(f"  Registros internos revisados : {len(self.pagos_recepcion)} recepción, {len(self.pagos_medilink)} Medilink")
        print(f"  Días auditados               : {len(cuadre)}")
        print(f"  Total ingresos informados    : ${total_int:>14,.0f}")
        print(f"  Total respaldado externo     : ${total_ext:>14,.0f}")
        print(f"  Diferencia neta              : ${total_int - total_ext:>14,.0f}")
        print(f"  Días que cuadran             : {n_ok}")
        print(f"  Días con observaciones       : {n_obs}")
        print(f"  Días que NO cuadran          : {n_no}")
        print(f"  Hallazgos totales            : {len(self.hallazgos)}")
        print(f"  Hallazgos prioridad ALTA     : {n_hall_alta}")
        print(f"  Nivel de riesgo detectado    : {nivel_riesgo}")

        # B. Resumen por medio de pago
        print()
        print("B. RESUMEN POR MEDIO DE PAGO")
        print(sep)
        medios = ["TRANSFERENCIA", "EFECTIVO", "TRANSBANK_DEBITO", "TRANSBANK_CREDITO"]
        fmt = "  {:<22} {:>14} {:>14} {:>14}  {}"
        print(fmt.format("Medio", "Registrado", "Respaldado", "Diferencia", "Estado"))
        print("  " + "-" * 76)
        for m in medios:
            ri = totales_int.get(m, 0.0)
            re = totales_ext.get(m, 0.0)
            df = ri - re
            st = "OK" if abs(df) < TOLERANCIA_MONTO else ("DIFERENCIA" if abs(df) < ri * 0.05 else "REVISAR")
            print(fmt.format(m, f"${ri:,.0f}", f"${re:,.0f}", f"${df:,.0f}", st))

        # C. Hallazgos detallados
        print()
        print("C. HALLAZGOS DETALLADOS")
        print(sep)
        if not self.hallazgos:
            print("  Sin hallazgos. Todo conciliado correctamente.")
        else:
            for i, h in enumerate(sorted(self.hallazgos, key=lambda x: (x.prioridad != "ALTA", x.fecha or date.min)), 1):
                fec_str = h.fecha.strftime("%d/%m/%Y") if h.fecha else "—"
                mi_str = f"${h.monto_interno:,.0f}" if h.monto_interno is not None else "—"
                me_str = f"${h.monto_externo:,.0f}" if h.monto_externo is not None else "—"
                print(f"  [{i:02d}] [{h.prioridad}] {h.tipo}")
                print(f"       Fecha: {fec_str} | Paciente: {h.paciente or '—'}")
                print(f"       Interno ({h.fuente_interna}): {mi_str}  |  Externo ({h.fuente_externa}): {me_str}")
                print(f"       {h.comentario}")
                print()

        # D. Cuadre diario
        print()
        print("D. CUADRE DIARIO")
        print(sep)
        if not cuadre:
            print("  Sin datos suficientes para cuadre diario.")
        else:
            fmt2 = "  {:<12} {:>10} {:>10} {:>10} {:>12} {:>10} {:>12}  {}"
            print(fmt2.format("Fecha", "Recepción", "Medilink", "Itaú", "BancoEstado", "Transbank", "Diferencia", "Estado"))
            print("  " + "-" * 90)
            for c in cuadre:
                print(fmt2.format(
                    c["fecha"],
                    f"${c['recepcion']:,.0f}",
                    f"${c['medilink']:,.0f}",
                    f"${c['itau']:,.0f}",
                    f"${c['banco_estado']:,.0f}",
                    f"${c['transbank']:,.0f}",
                    f"${c['diferencia']:,.0f}",
                    c["estado"],
                ))

        # E. Casos revisión manual
        revision = [h for h in self.hallazgos if h.tipo == "REQUIERE_REVISION_MANUAL"]
        print()
        print("E. CASOS PARA REVISIÓN MANUAL")
        print(sep)
        if not revision:
            print("  Ningún caso requiere revisión manual.")
        else:
            for i, h in enumerate(revision, 1):
                fec_str = h.fecha.strftime("%d/%m/%Y") if h.fecha else "—"
                print(f"  [{i}] {fec_str} — {h.medio}")
                print(f"      {h.comentario}")

        # F. Conclusión
        print()
        print("F. CONCLUSIÓN Y RECOMENDACIONES")
        print(sep)
        if nivel_riesgo == "BAJO":
            print("  Los registros concilian razonablemente. No se detectan diferencias materiales.")
        elif nivel_riesgo == "MEDIO":
            print("  Se detectaron diferencias menores. Revisar los hallazgos antes de cerrar el período.")
        else:
            print("  ATENCIÓN: Se detectaron diferencias materiales. Requiere revisión urgente.")

        if n_no > 0:
            print(f"  → {n_no} día(s) no cuadran. Priorizar revisión de esos días.")
        if any(h.tipo == "SIN_RESPALDO_BANCARIO" for h in self.hallazgos):
            print("  → Hay pagos registrados internamente sin respaldo bancario/Transbank.")
        if any(h.tipo == "SIN_RESPALDO_INTERNO" for h in self.hallazgos):
            print("  → Hay movimientos externos sin registro interno. Verificar si son pagos omitidos.")
        if any(h.tipo == "DUPLICADO" for h in self.hallazgos):
            print("  → Se detectaron posibles duplicados. Revisar antes de cerrar.")
        print()

    def exportar_html(self, ruta: str):
        """Exporta el informe como HTML con tablas navegables."""
        totales_int = self._totales_internos()
        totales_ext = self._totales_externos()
        cuadre = self._cuadre_diario()
        medios = ["TRANSFERENCIA", "EFECTIVO", "TRANSBANK_DEBITO", "TRANSBANK_CREDITO"]

        def color_estado(e):
            return {"CUADRA": "#2ecc71", "CUADRA CON OBSERVACIONES": "#f39c12", "NO CUADRA": "#e74c3c"}.get(e, "#aaa")

        def color_prio(p):
            return {"ALTA": "#e74c3c", "MEDIA": "#f39c12", "BAJA": "#2ecc71"}.get(p, "#aaa")

        rows_medio = ""
        for m in medios:
            ri = totales_int.get(m, 0.0)
            re = totales_ext.get(m, 0.0)
            df = ri - re
            st = "OK" if abs(df) < TOLERANCIA_MONTO else "REVISAR"
            cl = "#2ecc71" if st == "OK" else "#e74c3c"
            rows_medio += f"<tr><td>{m}</td><td>${ri:,.0f}</td><td>${re:,.0f}</td><td>${df:,.0f}</td><td style='color:{cl};font-weight:bold'>{st}</td></tr>\n"

        rows_hallazgos = ""
        for h in sorted(self.hallazgos, key=lambda x: (x.prioridad != "ALTA", x.fecha or date.min)):
            fec = h.fecha.strftime("%d/%m/%Y") if h.fecha else "—"
            mi = f"${h.monto_interno:,.0f}" if h.monto_interno is not None else "—"
            me = f"${h.monto_externo:,.0f}" if h.monto_externo is not None else "—"
            cp = color_prio(h.prioridad)
            rows_hallazgos += (
                f"<tr><td>{fec}</td><td>{h.paciente or '—'}</td><td>{mi}</td><td>{me}</td>"
                f"<td>{h.fuente_interna}</td><td>{h.fuente_externa}</td><td>{h.medio}</td>"
                f"<td>{h.tipo}</td><td>{h.comentario}</td>"
                f"<td style='color:{cp};font-weight:bold'>{h.prioridad}</td></tr>\n"
            )

        rows_diario = ""
        for c in cuadre:
            ce = color_estado(c["estado"])
            rows_diario += (
                f"<tr><td>{c['fecha']}</td><td>${c['recepcion']:,.0f}</td><td>${c['medilink']:,.0f}</td>"
                f"<td>${c['itau']:,.0f}</td><td>${c['banco_estado']:,.0f}</td><td>${c['transbank']:,.0f}</td>"
                f"<td>${c['diferencia']:,.0f}</td>"
                f"<td style='color:{ce};font-weight:bold'>{c['estado']}</td></tr>\n"
            )

        html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Informe Conciliación — CMC</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 2em; color: #222; }}
  h1 {{ color: #2c3e50; }}
  h2 {{ color: #2980b9; border-bottom: 2px solid #2980b9; padding-bottom: 4px; }}
  table {{ border-collapse: collapse; width: 100%; margin-bottom: 2em; font-size: 0.9em; }}
  th {{ background: #2c3e50; color: white; padding: 8px; text-align: left; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #ddd; }}
  tr:hover {{ background: #f5f5f5; }}
  .meta {{ color: #666; margin-bottom: 1em; }}
</style>
</head>
<body>
<h1>Informe de Conciliación Financiera</h1>
<p class="meta">Centro Médico Carampangue — Generado: {datetime.now().strftime('%d/%m/%Y %H:%M')}</p>

<h2>B. Resumen por medio de pago</h2>
<table>
<tr><th>Medio</th><th>Registrado</th><th>Respaldado</th><th>Diferencia</th><th>Estado</th></tr>
{rows_medio}
</table>

<h2>C. Hallazgos detallados ({len(self.hallazgos)})</h2>
<table>
<tr><th>Fecha</th><th>Paciente</th><th>Monto interno</th><th>Monto externo</th>
<th>Fuente interna</th><th>Fuente externa</th><th>Medio</th><th>Tipo</th><th>Comentario</th><th>Prioridad</th></tr>
{rows_hallazgos if rows_hallazgos else '<tr><td colspan="10">Sin hallazgos</td></tr>'}
</table>

<h2>D. Cuadre diario</h2>
<table>
<tr><th>Fecha</th><th>Recepción</th><th>Medilink</th><th>Itaú</th><th>BancoEstado</th><th>Transbank</th><th>Diferencia</th><th>Estado</th></tr>
{rows_diario if rows_diario else '<tr><td colspan="8">Sin datos</td></tr>'}
</table>
</body>
</html>"""

        with open(ruta, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nInforme HTML exportado: {ruta}")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Auditor de conciliación financiera — CMC")
    parser.add_argument("--desde", help="Fecha inicio YYYY-MM-DD")
    parser.add_argument("--hasta", help="Fecha fin YYYY-MM-DD")
    parser.add_argument("--output", help="Exportar informe HTML a este archivo")
    args = parser.parse_args()

    desde = date.fromisoformat(args.desde) if args.desde else None
    hasta = date.fromisoformat(args.hasta) if args.hasta else None

    auditor = Auditor(desde=desde, hasta=hasta)
    auditor.auditar()
    auditor.imprimir_informe()

    if args.output:
        auditor.exportar_html(args.output)


if __name__ == "__main__":
    main()
