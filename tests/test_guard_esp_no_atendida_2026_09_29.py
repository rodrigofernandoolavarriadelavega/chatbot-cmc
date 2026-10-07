"""El filtro de especialidades no atendidas buscaba subcadenas: "urolog" dentro
de "neurología" mandaba al CESFAM a quien preguntaba por el bono Fonasa
(caso real 2026-09-29, 3 casos en 30 días)."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from claude_helper import _validar_respuesta_faq, _MSG_ESP_NO_ATENDIDA

NO_DEBE_DERIVAR = [
    "¡Buena pregunta! Con bono Fonasa MLE: Medicina General, Neurología y Ginecología.",
    "La Dra. Franca González es neuróloga; Neurología es por telemedicina.",
    "Nuestra dentista es cirujano dentista y atiende Odontología General.",
    "La Dra. Burgos es cirujana-dentista.",
    "Hacemos odontopediatría con la dentista general.",
    "Otorrinolaringología con el Dr. Borrego.",
]
DEBE_DERIVAR = [
    "Para eso necesitas un urólogo.",
    "Debes ver a un dermatólogo.",
    "Te conviene un reumatólogo.",
]

def test_no_deriva_especialidades_que_si_tenemos():
    for t in NO_DEBE_DERIVAR:
        assert _validar_respuesta_faq(t) != _MSG_ESP_NO_ATENDIDA, t

def test_sigue_derivando_las_que_no_tenemos():
    for t in DEBE_DERIVAR:
        assert _validar_respuesta_faq(t) == _MSG_ESP_NO_ATENDIDA, t


# 2026-10-07: pediatría NO manda al CESFAM (regla del dueño: el CMC atiende niños).
def test_pediatria_ofrece_medicina_general_y_no_cesfam():
    r = _validar_respuesta_faq("Eso lo ve Pediatría.")
    assert r != _MSG_ESP_NO_ATENDIDA
    assert "CESFAM" not in r and "medicina general" in r.lower()

def test_pediatria_ya_aclarada_no_se_pisa():
    t = "No tenemos pediatra, pero nuestros médicos generales atienden niños. ¿Te agendo con Medicina General?"
    assert _validar_respuesta_faq(t) == t
