#!/usr/bin/env python3
"""Pre-puntúa fricción en conversaciones de ecografía y las reparte en lotes
para el portavión de agentes. Heurística determinista solo para PRIORIZAR;
el juicio fino lo hacen los agentes Claude.

Salida:
  data/eco_batches/batch_00.json ... (lotes priorizados por fricción)
  data/eco_batches/_manifest.json (resumen: cuántas, distribución, cobertura)
"""
import json, re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "eco_conversations.json"
OUT = ROOT / "data" / "eco_batches"
OUT.mkdir(exist_ok=True)

BATCH = 20          # conversaciones por lote (≈ agente)
TOP_DEEP = 360      # cuántas de mayor fricción van al review profundo

convs = json.load(open(SRC))

def norm(s): return re.sub(r"\s+", " ", (s or "").lower()).strip()

def score(c):
    msgs = c["msgs"]
    inb = [norm(m["t"]) for m in msgs if m["d"] == "in"]
    out = [norm(m["t"]) for m in msgs if m["d"] == "out"]
    states = [m.get("s") or "" for m in msgs]
    s = 0.0
    sig = []
    # 1) menú repetido (bot manda mismo bloque largo >=2 veces)
    longouts = [o for o in out if len(o) > 120]
    if len(longouts) - len(set(longouts)) >= 1:
        s += 3; sig.append("menu_repetido")
    # 2) takeover / derivación a humano
    if any("HUMAN" in st or "TAKEOVER" in st for st in states):
        s += 2; sig.append("takeover")
    # 3) confusión explícita del paciente
    conf = ("no entiendo", "no entendi", "no cacho", "como asi", "como es",
            "que significa", "no me queda claro", "ah?", "ahh", "?", "como hago")
    if any(any(k in t for k in conf) for t in inb):
        s += 1.5; sig.append("confusion_paciente")
    # 4) mamaria mal ruteada a ginecología (Rejón) o mezcla
    txt = " ".join(inb + out)
    if "mamaria" in txt or "mamas" in txt or "de mama" in txt:
        if "rejon" in txt or "rejón" in txt or "ginec" in txt or "transvaginal" in txt:
            s += 4; sig.append("mamaria_x_ginecologia")
    # 5) confusión transvaginal / embarazo / obstétrica (CMC no hace obstétrica)
    if any(k in txt for k in ("embaraz", "obstetric", "prenatal", "guagua", "wawa", "bebe", "bb")):
        if "eco" in txt or "ecograf" in txt:
            s += 2; sig.append("obstetrica_no_disponible")
    # 6) precio mencionado y luego silencio del paciente (abandono)
    price_idx = next((i for i, m in enumerate(msgs)
                      if m["d"] == "out" and re.search(r"\$?\s*\d{2}\.?\d{3}", m["t"] or "")), None)
    if price_idx is not None and price_idx >= len(msgs) - 2 and msgs[-1]["d"] == "out":
        s += 2; sig.append("abandono_post_precio")
    # 7) última es del paciente (posible no respondida)
    if msgs and msgs[-1]["d"] == "in":
        s += 1; sig.append("ultima_inbound")
    # 8) conversación larga (ida y vuelta = fricción)
    if len(msgs) >= 14:
        s += 1.5; sig.append("hilo_largo")
    elif len(msgs) >= 8:
        s += 0.5
    # 9) eco-bleed sospechoso: parte del cuerpo sin raíz "eco" pero ruteado
    if any(k in txt for k in ("rodilla", "diente", "muela", "pie", "tobillo")) and "eco" in txt:
        s += 1; sig.append("posible_ecobleed")
    return round(s, 2), sig

for c in convs:
    c["score"], c["sig"] = score(c)

convs.sort(key=lambda c: (-c["score"], -c["n"]))

deep = convs[:TOP_DEEP]
batches = [deep[i:i + BATCH] for i in range(0, len(deep), BATCH)]
for i, b in enumerate(batches):
    json.dump(b, open(OUT / f"batch_{i:02d}.json", "w"), ensure_ascii=False)

# distribución de señales
from collections import Counter
sigcount = Counter()
for c in deep:
    for x in c["sig"]:
        sigcount[x] += 1

manifest = {
    "total_convs": len(convs),
    "total_msgs": sum(c["n"] for c in convs),
    "deep_review": len(deep),
    "batches": len(batches),
    "batch_size": BATCH,
    "dropped_low_friction": len(convs) - len(deep),
    "score_max": convs[0]["score"] if convs else 0,
    "score_at_cut": deep[-1]["score"] if deep else 0,
    "signal_counts_top": dict(sigcount.most_common()),
}
json.dump(manifest, open(OUT / "_manifest.json", "w"), ensure_ascii=False, indent=2)
print(json.dumps(manifest, ensure_ascii=False, indent=2))
