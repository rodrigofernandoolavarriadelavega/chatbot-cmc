"""Agente Creativos — genera piezas de marketing segun demanda detectada.

Riesgo MEDIO: solo opera sobre archivos locales (galeria) y llama a OpenAI.
No contacta pacientes ni escribe en Medilink.

Fuentes reusadas:
  - session.py (conversation_events event='sin_disponibilidad')  → demanda no satisfecha
  - autopilot.designs.load_designs / add_design                  → galeria de piezas
  - autopilot.image_gen.build_prompt / generate_png              → generacion OpenAI
  - autopilot.ad_formats                                         → formatos de pieza
"""
import logging
import os
from datetime import datetime, timezone

from ..base import Agent, AgentAction

log = logging.getLogger("bot")

# Dias de ventana para considerar demanda "reciente"
_DEMANDA_DIAS = int(os.getenv("ALMA_AGENT_CREATIVOS_DIAS", "30"))
# Horas minimas que debe tener un creativo reciente para no volver a generar
_CREATIVO_RECIENTE_HORAS = int(os.getenv("ALMA_AGENT_CREATIVOS_RECIENTE_HORAS", "72"))
# Max especialidades por corrida (una pieza por especialidad)
_MAX_ESPECIALIDADES = int(os.getenv("ALMA_AGENT_CREATIVOS_MAX", "3"))


def _demanda_caliente(dias: int, limite: int) -> list[dict]:
    """Especialidades con mas demanda no satisfecha reciente, desde sessions.db.
    Degrada a [] si falla. Misma logica que yield_agenda._candidatos_demanda_reprimida
    pero agrupada por especialidad (no por paciente)."""
    try:
        from session import _conn
        conn = _conn()
    except Exception:  # noqa: BLE001
        return []
    try:
        cur = conn.execute(
            """
            SELECT lower(json_extract(meta,'$.especialidad')) esp, COUNT(DISTINCT phone) n
            FROM conversation_events
            WHERE event='sin_disponibilidad' AND ts >= datetime('now', ?)
              AND json_extract(meta,'$.especialidad') IS NOT NULL
            GROUP BY esp
            ORDER BY n DESC
            LIMIT ?
            """,
            (f"-{dias} days", limite * 3),
        )
        return [{"especialidad": r[0], "n_pacientes": r[1]}
                for r in cur.fetchall() if r[0]]
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _especialidades_con_creativo_reciente(horas: int) -> set[str]:
    """Especialidades que ya tienen un creativo generado recientemente (borrador/aprobado).
    Evita generar piezas duplicadas. Degrada a set vacio si falla."""
    try:
        from autopilot.designs import load_designs
        designs = load_designs()
    except Exception:  # noqa: BLE001
        return set()

    cutoff = datetime.now(timezone.utc).timestamp() - horas * 3600
    recientes = set()
    for d in designs:
        created = d.get("created_at", "")
        esp = (d.get("specialty") or "").lower().strip()
        if not esp:
            continue
        try:
            ts = datetime.fromisoformat(created).timestamp()
            if ts >= cutoff:
                recientes.add(esp)
        except Exception:  # noqa: BLE001
            pass
    return recientes


class CreativosAgent(Agent):

    async def perceive(self) -> dict:
        demanda = _demanda_caliente(_DEMANDA_DIAS, _MAX_ESPECIALIDADES * 3)
        recientes = _especialidades_con_creativo_reciente(_CREATIVO_RECIENTE_HORAS)
        return {"demanda": demanda, "recientes": recientes}

    async def decide(self, ctx: dict) -> list[AgentAction]:
        demanda = ctx.get("demanda", [])
        recientes = ctx.get("recientes", set())
        out = []
        for item in demanda:
            esp = item.get("especialidad", "")
            if not esp:
                continue
            if esp in recientes:
                continue
            # Capitalize para el titulo
            esp_label = esp.title()
            out.append(AgentAction(
                kind="generar_creativo",
                summary=f"Generar pieza de marketing para {esp_label} ({item['n_pacientes']} con demanda reprimida)",
                risk="medio",
                target=None,
                params={
                    "especialidad": esp,
                    "especialidad_label": esp_label,
                    "n_pacientes": item["n_pacientes"],
                },
                requires_contact=False,
                requires_medilink_write=False,
            ))
            if len(out) >= _MAX_ESPECIALIDADES:
                break
        return out

    async def execute_one(self, action: AgentAction) -> dict:
        esp = action.params.get("especialidad", "")
        esp_label = action.params.get("especialidad_label", esp.title())
        n = action.params.get("n_pacientes", 0)

        try:
            from autopilot.image_gen import build_prompt, generate_png, gpt_size_for
            from autopilot.designs import add_design
        except Exception as e:  # noqa: BLE001
            log.error("creativos: no se pudo importar image_gen/designs: %s", e)
            return {"ok": False, "error": str(e)}

        # Formato por defecto: instagram_post cuadrado
        fmt = {"label": "Instagram Post", "ratio": "1:1", "width": 1024, "height": 1024,
               "cta": "Agenda tu hora por WhatsApp"}
        try:
            from autopilot.ad_formats import FORMATS
            # Preferir feed_square o instagram si existe
            for candidate in ("feed_square", "instagram_post", "story"):
                if candidate in FORMATS:
                    fmt = FORMATS[candidate]
                    break
        except Exception:  # noqa: BLE001
            pass

        prompt = build_prompt(
            fmt,
            title=f"{esp_label} en Carampangue",
            subtitle=f"Atencion especializada para toda la Provincia de Arauco",
            specialty=esp_label,
            brief=(
                f"Pieza de inversion para capturar demanda real: "
                f"{n} pacientes buscaron {esp_label} recientemente y no encontraron cupo."
            ),
        )

        try:
            size = gpt_size_for(fmt)
            png_bytes = await generate_png(prompt, size=size, quality="high")
        except Exception as e:  # noqa: BLE001
            log.warning("creativos: generate_png fallo para %s: %s", esp, e)
            return {"ok": False, "error": str(e)}

        # Guardar PNG en /static/ad_designs/
        slug = esp.replace(" ", "_").replace("/", "_")[:32]
        ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        filename = f"agente_{slug}_{ts_str}.png"
        static_dir = os.path.join("static", "ad_designs")
        os.makedirs(static_dir, exist_ok=True)
        filepath = os.path.join(static_dir, filename)
        try:
            with open(filepath, "wb") as f:
                f.write(png_bytes)
            image_url = f"/static/ad_designs/{filename}"
        except Exception as e:  # noqa: BLE001
            log.warning("creativos: no se pudo guardar PNG en disco: %s", e)
            image_url = ""

        record = add_design({
            "id": f"agente_{slug}_{ts_str}",
            "title": f"{esp_label} en Carampangue",
            "specialty": esp_label,
            "format": fmt.get("label", "Instagram Post"),
            "image_url": image_url,
            "status": "borrador",
            "generated_by": "alma_agent_creativos",
            "demanda_n": n,
        })
        log.info("creativos: pieza generada para %s → %s (borrador)", esp_label, image_url)
        return {"ok": True, "design_id": record.get("id"), "image_url": image_url, "especialidad": esp}


AGENT = CreativosAgent(
    id="creativos",
    name="Generador de Creativos",
    descr=(
        "Genera piezas de marketing en estado borrador para especialidades con "
        "demanda no satisfecha reciente. No contacta pacientes."
    ),
    risk="medio",
    flag="ALMA_AGENT_CREATIVOS",
    category="marketing",
    schedule={"hour": 8, "minute": 0},
)
