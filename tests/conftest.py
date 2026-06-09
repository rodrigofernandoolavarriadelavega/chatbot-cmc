"""Guarda de colección de pytest para tests/.

La carpeta tests/ mezcla DOS cosas que pytest no puede tratar igual:

  1. Tests pytest reales: definen `def test_...` (o clases unittest) y son
     seguros de importar.
  2. Scripts standalone: se corren como `python tests/x.py` y ejecutan su
     lógica EN TIEMPO DE IMPORT — algunos con side-effects peligrosos
     (levantan un TestClient y pegan a endpoints, hacen `sys.exit()`, leen
     el .env de PRODUCCIÓN, o usan la Meta API real y envían WhatsApp).

pytest importa cada `test_*.py` durante la colección. Importar un script del
grupo (2) contamina o ABORTA la corrida entera (INTERNALERROR → "no tests ran")
y, peor, podría disparar acciones reales. Antes de esta guarda, `pytest tests/`
no corría NADA por culpa de `test_agendador_e2e.py` (sys.exit al importar) y
`test_winback_e2e.py` (lee /opt/chatbot-cmc/.env al importar).

Estrategia auto-mantenida (cero lista que se pudra):
  - Ignoramos cualquier `test_*.py` que NO contenga `def test_` → es un script,
    no un test; pytest nunca corrió sus asserts de todos modos.
  - Más una lista explícita de archivos que SÍ tienen `def test_` pero están
    acoplados a producción y no deben importarse jamás en local.

Si agregás un test pytest de verdad, se colecciona solo. Si agregás un harness
standalone (`test_foo.py` sin `def test_`), se ignora solo.

Para correr un script standalone, invocalo directo: `python tests/<archivo>.py`.
"""
from pathlib import Path

_HERE = Path(__file__).parent

# Tienen `def test_` pero leen el .env de prod / usan Meta API real / envían
# WhatsApp de verdad. NUNCA importar en local — pytest los dispararía al colectar.
_PROD_COUPLED = {"test_winback_e2e.py"}

collect_ignore = []
for _f in sorted(_HERE.glob("test_*.py")):
    _txt = _f.read_text(encoding="utf-8", errors="ignore")
    if _f.name in _PROD_COUPLED or "def test_" not in _txt:
        collect_ignore.append(_f.name)
