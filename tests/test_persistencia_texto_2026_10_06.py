"""Texto del 2º toque de persistencia: el campo especialidad puede ser el
apellido del profesional ("abarca") o venir en minúscula — se normaliza."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
from persistencia import _esp_para_mensaje as f  # noqa: E402

CASOS = {"abarca": " con *Dr. Andrés Abarca*", "marquez": " con *Dr. Alonso Márquez*",
         "kinesiología": " de *Kinesiología*", "medicina general": " de *Medicina General*",
         "psicología adulto": " de *Psicología*", "obstetrica": " de *Ecografía obstétrica*",
         "no especificada": "", "": ""}
fallas = [k for k, v in CASOS.items() if f(k) != v]
for k in CASOS:
    print(("OK  " if k not in fallas else "FAIL") + f" {k!r} → {f(k)!r}")
print(f"\n{len(fallas)} fallas")
sys.exit(1 if fallas else 0)
