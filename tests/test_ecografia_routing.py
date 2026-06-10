"""Tests del routing centralizado de ecografías (app/ecografias.py).

Verifica:
  - 12 ecografías ginecológicas → Rejón (ID 61, $35.000)
  - 5 variantes de ecocardiograma → Millán waitlist (ID 60, $110.000)
  - 10 ecografías generales → Pardo (ID 68, $40.000)
  - 3 sin especificar → route_ecografia retorna None
  - Integración con detect_intent (sin tocar Claude Haiku)
  - Integración con _detectar_especialidad_en_texto

Uso:
  pytest tests/test_ecografia_routing.py -v
  python tests/test_ecografia_routing.py
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

os.environ.setdefault("SQLCIPHER_KEY", "")


class TestRouteEcografiaGinecologia(unittest.TestCase):
    """Ecografías ginecológicas/obstétricas → Rejón (ID 61, $35.000).

    OJO: mamaria NO va acá — es partes blandas → Pardo (ID 68). Los tests de
    mamaria de esta clase usan _assert_pardo_mamaria por razones históricas.
    """

    def _route(self, texto: str):
        from ecografias import route_ecografia
        return route_ecografia(texto)

    def _assert_rejon(self, texto: str, msg: str = ""):
        r = self._route(texto)
        label = msg or repr(texto)
        self.assertIsNotNone(r, f"{label}: route_ecografia no debe retornar None")
        self.assertEqual(r["id_profesional"], 61, f"{label}: debe ser Rejón (ID 61)")
        self.assertEqual(r["especialidad_destino"], "ginecología", f"{label}: especialidad debe ser ginecología")
        self.assertEqual(r["precio_particular"], 35000, f"{label}: precio debe ser $35.000")

    def test_transvaginal(self):
        self._assert_rejon("transvaginal")

    def test_eco_transvaginal(self):
        self._assert_rejon("eco transvaginal")

    def test_ecografia_transvaginal(self):
        self._assert_rejon("ecografía transvaginal")

    def test_ecografia_transvaginal_sin_tilde(self):
        self._assert_rejon("ecografia transvaginal")

    def test_transvajinal_typo(self):
        self._assert_rejon("ecografia transvajinal")

    def test_endovaginal(self):
        self._assert_rejon("endovaginal")

    def test_eco_pelvica(self):
        self._assert_rejon("eco pelvica")

    def test_ecografia_pelvica_tilde(self):
        self._assert_rejon("ecografía pélvica")

    # NOTA: mamaria NO es ginecológica — es partes blandas y la realiza David
    # Pardo (ID 68, $40.000). Fix 2026-05-22 (676239f+4906130). Estos tests se
    # corrigieron el 2026-06-07: antes afirmaban Rejón y quedaron obsoletos tras
    # ese fix. Ver app/ecografias.py (ecografia_general_pardo) y CLAUDE.md.
    def _assert_pardo_mamaria(self, texto: str):
        r = self._route(texto)
        self.assertIsNotNone(r, f"{texto!r}: no debe retornar None")
        self.assertEqual(r["id_profesional"], 68, f"{texto!r}: mamaria → Pardo (ID 68), es partes blandas")
        self.assertEqual(r["especialidad_destino"], "ecografía", f"{texto!r}: destino ecografía")
        self.assertEqual(r["precio_particular"], 40000, f"{texto!r}: $40.000")

    def test_eco_mamaria(self):
        self._assert_pardo_mamaria("eco mamaria")

    def test_ecotomografia_mamaria(self):
        self._assert_pardo_mamaria("ecotomografía mamaria")

    def test_eco_de_mamas(self):
        self._assert_pardo_mamaria("eco de mamas")

    # CORRECCIÓN 2026-06-09: eco obstétrica/prenatal/embarazo → NO disponible en CMC
    # (antes iban a Rejón, lo cual era incorrecto — CMC no hace eco de embarazo).
    # Las keywords obstétricas ahora van a "obstetrica_no_disponible" (flujo: no_disponible).
    def test_ecografia_obstetrica_no_disponible(self):
        from ecografias import route_ecografia
        r = route_ecografia("ecografía obstétrica")
        self.assertIsNotNone(r)
        self.assertEqual(r["flujo"], "no_disponible",
                         "eco obstétrica debe tener flujo=no_disponible (CMC no la realiza)")
        self.assertEqual(r["especialidad_destino"], "obstetrica")

    def test_ecografia_prenatal_no_disponible(self):
        from ecografias import route_ecografia
        r = route_ecografia("eco prenatal")
        self.assertIsNotNone(r)
        self.assertEqual(r["flujo"], "no_disponible",
                         "eco prenatal debe tener flujo=no_disponible (CMC no la realiza)")

    def test_ecografia_embarazo_no_disponible(self):
        from ecografias import route_ecografia
        r = route_ecografia("ecografía de embarazo")
        self.assertIsNotNone(r)
        self.assertEqual(r["flujo"], "no_disponible",
                         "eco de embarazo debe tener flujo=no_disponible (CMC no la realiza)")

    def test_ecografia_de_ovarios(self):
        self._assert_rejon("ecografía de ovarios")

    def test_ecografia_de_utero(self):
        self._assert_rejon("ecografia de utero")

    def test_frase_larga_transvaginal(self):
        self._assert_rejon("necesito agendar una ecografía transvaginal urgente")

    def test_eco_ginecologica(self):
        self._assert_rejon("ecografia ginecologica")


class TestRouteEcografiaCardiologia(unittest.TestCase):
    """Ecocardiograma → Millán waitlist (ID 60, $110.000)."""

    def _route(self, texto: str):
        from ecografias import route_ecografia
        return route_ecografia(texto)

    def _assert_millan(self, texto: str, msg: str = ""):
        r = self._route(texto)
        label = msg or repr(texto)
        self.assertIsNotNone(r, f"{label}: route_ecografia no debe retornar None")
        self.assertEqual(r["id_profesional"], 60, f"{label}: debe ser Millán (ID 60)")
        self.assertEqual(r["especialidad_destino"], "ecocardiograma", f"{label}: especialidad debe ser ecocardiograma")
        self.assertEqual(r["flujo"], "waitlist", f"{label}: flujo debe ser waitlist")
        self.assertEqual(r["precio_particular"], 110000, f"{label}: precio debe ser $110.000")

    def test_ecocardiograma(self):
        self._assert_millan("ecocardiograma")

    def test_eco_cardiograma_separado(self):
        self._assert_millan("eco cardiograma")

    def test_ecografia_cardiaca(self):
        self._assert_millan("ecografia cardiaca")

    def test_doppler_cardiaco(self):
        self._assert_millan("doppler cardiaco")

    def test_eco_del_corazon(self):
        self._assert_millan("eco del corazon")

    def test_ultrasonido_del_corazon(self):
        self._assert_millan("ultrasonido del corazon")

    def test_eco_al_corazon(self):
        self._assert_millan("eco al corazon")

    def test_examen_ecocardiograma(self):
        self._assert_millan("examen ecocardiograma")

    def test_frase_larga_ecocardiograma(self):
        self._assert_millan("me mandaron hacer un ecocardiograma para ver el corazon")

    def test_eco_corazon_tilde(self):
        self._assert_millan("eco corazón")


class TestRouteEcografiaGeneral(unittest.TestCase):
    """Ecografías generales → Pardo (ID 68, $40.000)."""

    def _route(self, texto: str):
        from ecografias import route_ecografia
        return route_ecografia(texto)

    def _assert_pardo(self, texto: str, msg: str = ""):
        r = self._route(texto)
        label = msg or repr(texto)
        self.assertIsNotNone(r, f"{label}: route_ecografia no debe retornar None")
        self.assertEqual(r["id_profesional"], 68, f"{label}: debe ser Pardo (ID 68)")
        self.assertEqual(r["especialidad_destino"], "ecografía", f"{label}: especialidad debe ser ecografía")
        self.assertEqual(r["precio_particular"], 40000, f"{label}: precio debe ser $40.000")

    def test_abdominal(self):
        self._assert_pardo("ecografia abdominal")

    def test_renal(self):
        self._assert_pardo("ecografía renal")

    def test_tiroides(self):
        self._assert_pardo("ecografía tiroides")

    def test_tiroidea(self):
        self._assert_pardo("ecografía tiroidea")

    def test_testicular(self):
        self._assert_pardo("eco testicular")

    def test_hepatica(self):
        self._assert_pardo("ecografía hepática")

    def test_vesicula(self):
        self._assert_pardo("eco vesicula")

    def test_partes_blandas(self):
        self._assert_pardo("ecografía partes blandas")

    def test_prostata(self):
        self._assert_pardo("eco prostata")

    def test_vesical(self):
        self._assert_pardo("ecografía vesical")

    def test_inguinal(self):
        self._assert_pardo("ecografia inguinal")

    def test_doppler_generico(self):
        self._assert_pardo("eco doppler")

    def test_frase_larga_abdominal(self):
        self._assert_pardo("me dijeron que tengo que hacerme una eco abdominal")


class TestRouteEcografiaLexicoNuevo(unittest.TestCase):
    """Keywords agregados en 2026-06-10: muslo, pierna, lumbar, omóplato, glúteos,
    pared abdominal, ecotomografía, renovesical, cervical, ecomamaria.
    Todos deben ir a David Pardo (ID 68, $40.000, particular).
    """

    def _route(self, texto: str):
        from ecografias import route_ecografia
        return route_ecografia(texto)

    def _assert_pardo(self, texto: str, msg: str = ""):
        r = self._route(texto)
        label = msg or repr(texto)
        self.assertIsNotNone(r, f"{label}: route_ecografia no debe retornar None")
        self.assertEqual(r["id_profesional"], 68, f"{label}: debe ser Pardo (ID 68)")
        self.assertEqual(r["especialidad_destino"], "ecografía", f"{label}: especialidad debe ser ecografía")
        self.assertEqual(r["precio_particular"], 40000, f"{label}: precio debe ser $40.000")

    # ── Muslo / pierna / pantorrilla ──────────────────────────────────────────
    def test_eco_muslo(self):
        self._assert_pardo("eco tomografia de muslo")

    def test_ecografia_muslo(self):
        self._assert_pardo("ecografia muslo")

    def test_eco_muslo_sin_eco(self):
        """'muslo' solo (sin raíz eco) no debe enrutar — gate de contexto."""
        from ecografias import route_ecografia
        r = route_ecografia("me duele el muslo")
        self.assertIsNone(r, "muslo sin contexto eco debe retornar None (gate)")

    def test_ecografia_pierna(self):
        self._assert_pardo("ecografia pierna")

    def test_ecografia_pantorrilla(self):
        self._assert_pardo("ecografia pantorrilla")

    # ── Lumbar ────────────────────────────────────────────────────────────────
    def test_ecografia_lumbar(self):
        self._assert_pardo("ecografia lumbar")

    def test_ecotomografia_lumbar(self):
        self._assert_pardo("ecotomografia lumbar")

    def test_eco_lumbar(self):
        self._assert_pardo("eco de lumbar")

    # ── Omóplato / escápula ──────────────────────────────────────────────────
    def test_ecografia_omoplato(self):
        self._assert_pardo("ecografia omoplato")

    def test_eco_escapula(self):
        self._assert_pardo("eco escapula")

    def test_frase_omoplato(self):
        self._assert_pardo("quiero una ecografia de omoplato y brazo")

    # ── Glúteos ───────────────────────────────────────────────────────────────
    def test_ecografia_gluteos(self):
        self._assert_pardo("ecografia gluteos")

    def test_ecotomografia_gluteos_bilateral(self):
        self._assert_pardo("ecotomografia gluteos bilateral")

    # ── Pared abdominal ──────────────────────────────────────────────────────
    def test_pared_abdominal(self):
        self._assert_pardo("ecografia pared abdominal")

    # ── Cervical ─────────────────────────────────────────────────────────────
    def test_ecografia_cervical(self):
        self._assert_pardo("ecografia cervical")

    # ── Ecotomografía (alias raíz sin tilde) ─────────────────────────────────
    def test_ecotomografia_raiz(self):
        """ecotomografia a secas → debe retornar None (preguntar tipo)."""
        from ecografias import route_ecografia
        r = route_ecografia("ecotomografia")
        self.assertIsNone(r, "ecotomografia sin órgano debe preguntar tipo (None)")

    def test_ecotomografia_con_organo(self):
        self._assert_pardo("ecotomografia hombro")

    def test_ecotomografia_con_organo_tobillo(self):
        self._assert_pardo("ecotomografia de tobillo")

    # ── Renovesical / typos ───────────────────────────────────────────────────
    def test_renovesical(self):
        self._assert_pardo("eco renovesical")

    def test_renovecical_typo(self):
        self._assert_pardo("eco renovecical")

    def test_reno_vesical_separado(self):
        self._assert_pardo("ecografia reno vesical")

    # ── Ecomamaria pegado ────────────────────────────────────────────────────
    def test_ecomamaria_pegado(self):
        """'ecomamaria' sin espacio debe ir a Pardo (partes blandas, NO Rejón)."""
        self._assert_pardo("ecomamaria")

    def test_ecomamaria_pegado_no_es_rejon(self):
        from ecografias import route_ecografia
        r = route_ecografia("ecomamaria")
        self.assertIsNotNone(r)
        self.assertNotEqual(r["id_profesional"], 61, "ecomamaria NO debe ir a Rejón")

    # ── eco grafia con espacio ────────────────────────────────────────────────
    def test_eco_grafia_con_espacio(self):
        self._assert_pardo("eco grafia abdominal")

    # ── Assume_context: respuestas cortas sin raíz eco ────────────────────────
    def test_assume_context_muslo(self):
        """Con assume_context=True, 'muslo' solo debe enrutar a Pardo."""
        from ecografias import route_ecografia
        r = route_ecografia("muslo", assume_context=True)
        self.assertIsNotNone(r, "muslo con assume_context debe enrutar")
        self.assertEqual(r["id_profesional"], 68)

    def test_assume_context_lumbar(self):
        from ecografias import route_ecografia
        r = route_ecografia("lumbar", assume_context=True)
        self.assertIsNotNone(r)
        self.assertEqual(r["id_profesional"], 68)

    def test_assume_context_pierna(self):
        from ecografias import route_ecografia
        r = route_ecografia("pierna", assume_context=True)
        self.assertIsNotNone(r)
        self.assertEqual(r["id_profesional"], 68)


class TestFaqEcografiaFonasa(unittest.TestCase):
    """FAQ local: eco + fonasa debe retornar mensaje claro (solo particular)."""

    def _fallback(self, texto: str):
        # Importar directamente sin deps de red
        import unicodedata as _u
        tl = texto.lower()
        tl_na = ''.join(c for c in _u.normalize('NFD', tl) if _u.category(c) != 'Mn')
        from claude_helper import _FAQ_LOCAL_FALLBACKS
        for keywords, respuesta in _FAQ_LOCAL_FALLBACKS:
            if all(k in tl_na for k in keywords):
                return respuesta
        return None

    def test_eco_fonasa_hit(self):
        r = self._fallback("ecografia con fonasa")
        self.assertIsNotNone(r, "eco+fonasa debe tener hit en FAQ local")
        self.assertIn("particular", r.lower())
        self.assertIn("40.000", r)

    def test_ecotomografia_fonasa_hit(self):
        r = self._fallback("ecotomografia bono fonasa")
        self.assertIsNotNone(r, "ecotomografia+fonasa debe tener hit")
        self.assertIn("particular", r.lower())

    def test_ecografia_bono_hit(self):
        r = self._fallback("puedo usar el bono para la ecografia")
        self.assertIsNotNone(r, "eco+bono debe tener hit en FAQ local")
        self.assertIn("particular", r.lower())

    def test_sin_eco_sin_hit(self):
        """Medicina general + Fonasa no debe caer en el hit de eco."""
        r = self._fallback("medicina general con fonasa")
        # Si hay hit, no debe ser el mensaje de ecografía
        if r:
            self.assertNotIn("David Pardo", r)


class TestRouteEcografiaSinEspecificar(unittest.TestCase):
    """Sin tipo especificado → None (caller debe preguntar tipo)."""

    def _route(self, texto: str):
        from ecografias import route_ecografia
        return route_ecografia(texto)

    def test_solo_ecografia(self):
        self.assertIsNone(self._route("ecografía"))

    def test_solo_ecografia_sin_tilde(self):
        self.assertIsNone(self._route("ecografia"))

    def test_una_eco(self):
        self.assertIsNone(self._route("una eco"))

    def test_necesito_eco(self):
        self.assertIsNone(self._route("necesito eco"))

    def test_texto_sin_ecografia(self):
        """Texto que no menciona ecografía → None."""
        self.assertIsNone(self._route("consulta médica"))

    def test_vacio(self):
        self.assertIsNone(self._route(""))


class TestTextoMencionaEcografia(unittest.TestCase):
    """texto_menciona_ecografia() detecta correctamente."""

    def _tiene(self, texto: str) -> bool:
        from ecografias import texto_menciona_ecografia
        return texto_menciona_ecografia(texto)

    def test_ecografia_detectada(self):
        self.assertTrue(self._tiene("ecografía abdominal"))

    def test_eco_solo(self):
        self.assertTrue(self._tiene("una eco"))

    def test_transvaginal_sin_eco(self):
        self.assertTrue(self._tiene("transvaginal"))

    def test_ecocardiograma(self):
        self.assertTrue(self._tiene("ecocardiograma"))

    def test_no_ecografia(self):
        self.assertFalse(self._tiene("consulta médica"))

    def test_eco_en_economico_no_contamina(self):
        """'eco' en 'económico' no debe dar True."""
        self.assertFalse(self._tiene("consulta económica"))


class TestIntegracionDetectarEspecialidad(unittest.TestCase):
    """_detectar_especialidad_en_texto respeta el routing de ecografias."""

    def _detect(self, texto: str):
        from flows import _detectar_especialidad_en_texto
        return _detectar_especialidad_en_texto(texto)

    def test_ecografia_generico(self):
        r = self._detect("ecografía")
        self.assertEqual(r, "ecografía")

    def test_ecotomografia(self):
        r = self._detect("ecotomografia")
        self.assertEqual(r, "ecografía")

    def test_ultrasonido_generico(self):
        r = self._detect("ultrasonido")
        self.assertEqual(r, "ecografía")

    def test_ecocardiograma_via_frases(self):
        """ecocardiograma ya no está en _FRASES_ESPECIALIDAD — debe retornar None o ecografía."""
        # Después del refactor, "ecocardiograma" no está en _FRASES_ESPECIALIDAD.
        # _detectar_especialidad_en_texto retornará "ecografía" por el prefijo "ecograf"
        # o None. Lo importante es que detect_intent luego llame route_ecografia.
        r = self._detect("ecocardiograma")
        # Puede ser "ecografía" (por prefijo "ecograf") o None — ambos son aceptables
        # porque detect_intent intercepta antes con route_ecografia.
        self.assertIn(r, ("ecografía", None),
                      f"ecocardiograma debe mapear a 'ecografía' o None vía _detectar_especialidad, got: {r}")


class TestPrioridadGinecologiaVsGeneral(unittest.TestCase):
    """Ginecología tiene prioridad sobre ecografía general en keywords ambiguos."""

    def test_mamaria_va_a_pardo_no_rejon(self):
        # Mamaria es partes blandas → David Pardo (68), NO ginecología/Rejón (61).
        from ecografias import route_ecografia
        r = route_ecografia("eco mamaria")
        self.assertIsNotNone(r)
        self.assertEqual(r["id_profesional"], 68, "eco mamaria debe ir a Pardo (partes blandas)")
        self.assertNotEqual(r["id_profesional"], 61, "eco mamaria NO debe ir a Rejón")

    def test_pelvica_va_a_rejon_no_pardo(self):
        from ecografias import route_ecografia
        r = route_ecografia("eco pelvica")
        self.assertEqual(r["id_profesional"], 61)

    def test_abdominal_va_a_pardo_no_rejon(self):
        from ecografias import route_ecografia
        r = route_ecografia("ecografia abdominal")
        self.assertEqual(r["id_profesional"], 68)

    def test_ecocardiograma_no_es_ecografia_general(self):
        from ecografias import route_ecografia
        r = route_ecografia("ecocardiograma")
        self.assertEqual(r["id_profesional"], 60)
        self.assertNotEqual(r["id_profesional"], 68)


if __name__ == "__main__":
    unittest.main(verbosity=2)
