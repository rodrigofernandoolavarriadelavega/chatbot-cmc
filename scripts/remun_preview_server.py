#!/usr/bin/env python3
"""Servidor local de vista previa de Remuneraciones Pro (fuera del bot).

Sirve templates/alma_remuneraciones.html en http://127.0.0.1:8871/ y contesta
los endpoints del contrato (docs/remuneraciones_pro_contrato.md) desde los
fixtures JSON (datos reales de septiembre generados con el motor). Los POST/DELETE
se aplican EN MEMORIA: no toca la base de datos, ni Medilink, ni el VPS.

Uso:
    python3 scripts/remun_preview_server.py [--fixtures DIR] [--port 8871] [--sin-verificar]

Con --sin-verificar arranca mostrando el estado previo al barrido de Medilink;
"Verificar con Medilink" simula el progreso día a día (~1 s por día) y al
terminar pasa al fixture verificado.
"""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parent.parent
FIX_DEFAULT = Path("/private/tmp/claude-501/-Users-rodrigoolavarria/"
                   "e22b573e-da89-496a-8ed1-21ba6c381d37/scratchpad/remun_fixtures")
TOKEN = "preview"
RETENCION = 0.1525

# Texto WhatsApp: se reusa el del motor si las dependencias del bot están
# disponibles (venv del repo); si no, se usa el texto del fixture.
try:
    sys.path.insert(0, str(REPO / "app"))
    from remuneraciones_pro_routes import _texto_whatsapp, _texto_detalle_whatsapp  # type: ignore
except Exception:  # noqa: BLE001
    _texto_whatsapp = _texto_detalle_whatsapp = None


class Estado:
    def __init__(self, fx: Path, sin_verificar: bool):
        self.fx = fx
        self.lock = threading.Lock()
        self.mes_ok = self._leer("mes_2026-09.json")
        self.mes_sin = self._leer("mes_2026-09_SIN_VERIFICAR.json") or self.mes_ok
        self.verificado = not sin_verificar
        self.progreso = None          # (inicio, dias_total) mientras simula
        self.ajustes: dict = {}       # prof → [{id, monto, motivo}]
        self.pagado: dict = {}        # prof → (pagado, monto, at)
        self.pct: dict = {}           # prof → pct
        self.tel: dict = {}
        self.sig_id = 1000
        self.detalles: dict = {}
        self.envios: dict = {}        # prof → at
        self.decisiones: dict = {}    # (prof, clave_linea) → bool

    def _leer(self, nombre):
        p = self.fx / nombre
        return json.loads(p.read_text()) if p.exists() else None

    def detalle(self, prof: int, mes: str):
        nombre = f"detalle_{prof}_{mes}.json"
        if nombre not in self.detalles:
            self.detalles[nombre] = self._leer(nombre)
        d = self.detalles[nombre]
        if d is None:
            return None
        d = copy.deepcopy(d)
        if not self.verificado:
            # Mismo cálculo que el motor sin Medilink: aproximación para la vista.
            d["verificado_medilink"] = "sin_verificar"
            for x in d["lineas"]:
                x["verificado"] = False
        for x in d["lineas"]:
            dec = self.decisiones.get((prof, x.get("clave_linea")))
            if x["tipo"] == "solo_en_modulo" and dec is not None:
                x["decision"] = dec
                x["cuenta_para_pago"] = dec
                x["monto_pago"] = x["modulo"]["esperado"] if dec else 0
                x["efecto_pago"] = x["monto_pago"]
                x["explicacion"] += (" Decisión suya: se INCLUYE en el pago." if dec
                                     else " Decisión suya: se EXCLUYE del pago.")
        return d

    def _extra_decisiones(self, pid: int) -> int:
        """Lo que suman a la base las líneas solo_en_modulo incluidas a mano."""
        if not any(k[0] == pid for k in self.decisiones):
            return 0
        d = self.detalle(pid, (self.mes_ok or {}).get("mes", "2026-09")) or {"lineas": []}
        return sum(x["monto_pago"] for x in d["lineas"] if x["tipo"] == "solo_en_modulo" and x.get("decision"))

    def _recalcular(self, p: dict):
        pid = p["id"]
        liq = p["liquidacion"]
        extra = self._extra_decisiones(pid)
        if extra:
            liq["base"] += extra
            p["conciliacion"]["monto_dif_pago"] = p["conciliacion"].get("monto_dif_pago", 0) + extra
        if pid in self.envios:
            p["whatsapp_enviado_at"] = self.envios[pid]
        if (pid in self.pct or extra) and not p["fijo"]:
            p["pct"] = liq["pct"] = self.pct.get(pid, p["pct"])
            if pid in self.pct:
                p["pct_actualizado_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            liq["bruto"] = round(liq["base"] * liq["pct"] / 100)
            liq["liquido"] = liq["bruto"] if p["sin_retencion"] else round(liq["bruto"] * (1 - RETENCION))
            liq["retencion"] = liq["bruto"] - liq["liquido"]
        liq["ajustes"] = list(self.ajustes.get(pid, liq.get("ajustes", [])))
        liq["total_ajustes"] = sum(a["monto"] for a in liq["ajustes"])
        liq["a_transferir"] = liq["liquido"] + liq["total_ajustes"]
        if pid in self.pagado:
            p["pagado"], p["monto_pagado"], p["pagado_at"] = self.pagado[pid]
        if pid in self.tel:
            p["telefono"] = self.tel[pid]

    def mes(self, mes: str):
        base = self.mes_ok if self.verificado else self.mes_sin
        if base is None or base.get("mes") != mes:
            return {"mes": mes, "verificado_medilink": {"estado": "sin_verificar", "at": None, "dias_ok": 0,
                                                         "dias_total": 0}, "totales": {}, "patrones": [],
                    "profesionales": []}
        m = copy.deepcopy(base)
        for p in m["profesionales"]:
            self._recalcular(p)
        t = m["totales"]
        ps = m["profesionales"]
        t["bruto"] = sum(p["liquidacion"]["bruto"] for p in ps)
        t["liquido"] = sum(p["liquidacion"]["liquido"] for p in ps)
        t["total_ajustes"] = sum(p["liquidacion"]["total_ajustes"] for p in ps)
        t["a_transferir"] = sum(p["liquidacion"]["a_transferir"] for p in ps)
        t["pagado"] = sum((p["monto_pagado"] if p.get("monto_pagado") is not None
                           else p["liquidacion"]["a_transferir"]) for p in ps if p["pagado"])
        t["por_pagar"] = sum(p["liquidacion"]["a_transferir"] for p in ps if not p["pagado"])
        t["n_pagados"] = sum(1 for p in ps if p["pagado"])
        t["base_pago"] = sum(p["liquidacion"]["base"] for p in ps)
        t["monto_dif_pago"] = sum(p["conciliacion"].get("monto_dif_pago", 0) for p in ps)
        ps.sort(key=lambda p: (p["pagado"], -p["liquidacion"]["base"]))
        v = m["verificado_medilink"]
        if self.progreso:
            v.update(self.estado_verificacion(mes))
        return m

    def estado_verificacion(self, mes: str):
        base = self.mes_ok or {}
        total = (base.get("verificado_medilink") or {}).get("dias_total", 30)
        if self.progreso:
            t0, _ = self.progreso
            hechos = min(total, int(time.time() - t0))
            if hechos >= total:
                self.progreso = None
                self.verificado = True
            else:
                return {"mes": mes, "estado": "en_curso", "en_curso": True, "dias_ok": hechos,
                        "dias_total": total, "dias_error": 0, "dia_actual": f"{mes}-{hechos + 1:02d}",
                        "at": None, "error": None}
        src = self.mes_ok if self.verificado else self.mes_sin
        v = dict((src or {}).get("verificado_medilink") or {})
        v.update({"mes": mes, "en_curso": False, "dia_actual": None, "error": None})
        return v


EST: Estado | None = None


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        sys.stderr.write("[preview] " + fmt % a + "\n")

    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:  # noqa: BLE001
            return {}

    def _html(self):
        p = REPO / "templates" / "alma_remuneraciones.html"
        if not p.exists():
            b = ("<h1>Falta templates/alma_remuneraciones.html</h1><p>El front lo está construyendo "
                 "otro agente. Los endpoints ya responden: <a href='/api/remuneraciones/mes?mes=2026-09'>"
                 "/api/remuneraciones/mes?mes=2026-09</a></p>").encode()
        else:
            b = p.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _static(self, path: str):
        p = (REPO / path.lstrip("/")).resolve()
        if not str(p).startswith(str((REPO / "static").resolve())) or not p.is_file():
            return self._json({"detail": "no encontrado"}, 404)
        b = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(p))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        mes = q.get("mes") or "2026-09"
        if u.path in ("/", "/alma/remuneraciones"):
            return self._html()
        if u.path.startswith("/static/"):
            return self._static(u.path)
        with EST.lock:
            if u.path == "/api/remuneraciones/mes":
                return self._json(EST.mes(mes))
            if u.path == "/api/remuneraciones/verificar-estado":
                return self._json(EST.estado_verificacion(mes))
            if u.path == "/api/remuneraciones/detalle":
                d = EST.detalle(int(q.get("prof", 0)), mes)
                return self._json(d) if d else self._json({"detail": "sin detalle"}, 404)
            if u.path == "/api/remuneraciones/whatsapp":
                pid = int(q.get("prof", 0))
                m = EST.mes(mes)
                p = next((x for x in m["profesionales"] if x["id"] == pid), None)
                fx = EST._leer(f"whatsapp_{pid}_{mes}.json")
                if not p or not fx:
                    return self._json({"detail": "profesional sin liquidación"}, 404)
                if _texto_whatsapp:
                    ls = (EST.detalle(pid, mes) or {}).get("lineas", [])
                    fx["texto"] = _texto_whatsapp(p, ls, mes, True)
                    fx["texto_resumen"] = _texto_whatsapp(p, ls, mes, False)
                    fx["texto_detalle"] = _texto_detalle_whatsapp(ls, mes)
                fx["telefono"] = p["telefono"]
                fx["largo"] = len(fx["texto"])
                fx["dos_mensajes"] = fx["largo"] > 1800
                fx["whatsapp_enviado_at"] = p.get("whatsapp_enviado_at")
                return self._json(fx)
        return self._json({"detail": "no encontrado"}, 404)

    def do_POST(self):  # noqa: N802
        u = urlparse(self.path)
        b = self._body()
        with EST.lock:
            if u.path == "/api/remuneraciones/verificar-medilink":
                if EST.progreso:
                    return self._json({"detail": "Ya hay una verificación en curso"}, 409)
                EST.verificado = False
                EST.progreso = (time.time(), 30)
                return self._json({"ok": True, **EST.estado_verificacion(b.get("mes") or "2026-09")})
            if u.path == "/api/remuneraciones/whatsapp-enviado":
                at = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
                EST.envios[int(b.get("prof") or 0)] = at
                return self._json({"ok": True, "whatsapp_enviado_at": at})
            if u.path == "/api/remuneraciones/linea-decision":
                pid, clave = int(b.get("prof") or 0), str(b.get("clave_linea") or "")
                d = EST.detalle(pid, b.get("mes") or "2026-09") or {"lineas": []}
                ln = next((x for x in d["lineas"] if x.get("clave_linea") == clave), None)
                if ln is None:
                    return self._json({"detail": "línea no existe en este mes"}, 404)
                if ln["tipo"] != "solo_en_modulo":
                    return self._json({"detail": "solo se decide sobre líneas 'solo_en_modulo'"}, 400)
                if b.get("incluir") is None:
                    EST.decisiones.pop((pid, clave), None)
                else:
                    EST.decisiones[(pid, clave)] = bool(b["incluir"])
                return self._json({"ok": True, "clave_linea": clave, "decision": b.get("incluir")})
            if u.path == "/api/remuneraciones/ajuste":
                if not (b.get("motivo") or "").strip() or not b.get("monto"):
                    return self._json({"detail": "monto y motivo son obligatorios"}, 400)
                EST.sig_id += 1
                EST.ajustes.setdefault(int(b["prof"]), []).append(
                    {"id": EST.sig_id, "monto": int(round(float(b["monto"]))), "motivo": b["motivo"].strip()})
                return self._json({"ok": True, "id": EST.sig_id})
            if u.path == "/api/remuneraciones/pagado":
                pag = bool(b.get("pagado"))
                monto = b.get("monto_pagado")
                EST.pagado[int(b["prof"])] = (pag, int(monto) if (pag and monto not in (None, "")) else None,
                                              time.strftime("%Y-%m-%d %H:%M:%S") if pag else None)
                return self._json({"ok": True, "pagado": pag})
            if u.path == "/api/remuneraciones/config":
                pid = int(b.get("id") or 0)
                if b.get("pct") is not None:
                    EST.pct[pid] = int(b["pct"])
                if "telefono" in b:
                    EST.tel[pid] = re.sub(r"\D", "", str(b["telefono"] or ""))
                return self._json({"ok": True})
        return self._json({"detail": "no encontrado"}, 404)

    def do_DELETE(self):  # noqa: N802
        u = urlparse(self.path)
        m = re.match(r"^/api/remuneraciones/ajuste/(\d+)$", u.path)
        if m:
            aid = int(m.group(1))
            with EST.lock:
                for lst in EST.ajustes.values():
                    for a in list(lst):
                        if a["id"] == aid:
                            lst.remove(a)
                            return self._json({"ok": True})
                # Ajustes que venían en el fixture
                for p in (EST.mes_ok or {}).get("profesionales", []):
                    aj = p["liquidacion"].get("ajustes") or []
                    if any(a["id"] == aid for a in aj):
                        EST.ajustes[p["id"]] = [a for a in aj if a["id"] != aid]
                        return self._json({"ok": True})
            return self._json({"detail": "ajuste no existe"}, 404)
        return self._json({"detail": "no encontrado"}, 404)


def main():
    global EST
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=str(FIX_DEFAULT))
    ap.add_argument("--port", type=int, default=8871)
    ap.add_argument("--sin-verificar", action="store_true")
    a = ap.parse_args()
    EST = Estado(Path(a.fixtures), a.sin_verificar)
    if EST.mes_ok is None:
        sys.exit(f"No hay fixtures en {a.fixtures} (falta mes_2026-09.json)")
    print(f"Remuneraciones Pro (vista previa) → http://127.0.0.1:{a.port}/  "
          f"[{'sin verificar' if a.sin_verificar else 'verificado'}; WhatsApp "
          f"{'motor' if _texto_whatsapp else 'fixture'}]")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()


if __name__ == "__main__":
    main()
