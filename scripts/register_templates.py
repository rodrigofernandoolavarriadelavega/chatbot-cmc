"""
Registra los Message Templates de WhatsApp Business API en Meta.

Uso:
    PYTHONPATH=app:. python scripts/register_templates.py

Requiere en .env:
    META_ACCESS_TOKEN   — token del System User con permisos whatsapp_business_management
    META_WABA_ID        — ID de la WhatsApp Business Account

Ref: https://developers.facebook.com/docs/whatsapp/business-management-api/message-templates
"""
import json
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
WABA_ID = os.getenv("META_WABA_ID", "")

if not ACCESS_TOKEN or not WABA_ID:
    print("ERROR: META_ACCESS_TOKEN y META_WABA_ID deben estar en .env")
    sys.exit(1)

API_URL = f"https://graph.facebook.com/v22.0/{WABA_ID}/message_templates"
HEADERS = {"Authorization": f"Bearer {ACCESS_TOKEN}"}

# ──────────────────────────────────────────────────────────────────────────────
# Definición de templates
# ──────────────────────────────────────────────────────────────────────────────

TEMPLATES = [
    # 1. Recordatorio 24h con botones de confirmación (incluye nombre paciente + dirección)
    {
        "name": "recordatorio_cita",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 👋 Te recordamos tu cita en el *Centro Médico Carampangue*:\n\n"
                    "🏥 *{{2}}* — {{3}}\n"
                    "📅 *{{4}}* a las *{{5}}*\n"
                    "💳 {{6}}\n"
                    "📍 Monsalve esquina República, Carampangue\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad.\n\n"
                    "¿Nos confirmas tu asistencia?"
                ),
                "example": {
                    "body_text": [["Sergio Carrasco", "Odontología General",
                                   "Dra. Javiera Burgos Godoy",
                                   "Lunes 13 de abril", "10:00", "Particular"]]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Confirmo"},
                    {"type": "QUICK_REPLY", "text": "Cambiar hora"},
                    {"type": "QUICK_REPLY", "text": "No podre ir"},
                ],
            },
        ],
    },

    # 2. Recordatorio 2h antes (incluye nombre paciente)
    {
        "name": "recordatorio_cita_2h",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} ⏰ *En 2 horas* tienes tu cita en el *Centro Médico Carampangue*:\n\n"
                    "🏥 *{{2}}* — {{3}}\n"
                    "🕐 Hoy a las *{{4}}*\n"
                    "📍 Monsalve esquina República, Carampangue\n\n"
                    "Recuerda llegar *15 minutos antes* con tu cédula de identidad."
                ),
                "example": {
                    "body_text": [["Sergio Carrasco", "Kinesiología", "Luis Armijo", "14:00"]]
                },
            },
        ],
    },

    # 3. Post-consulta seguimiento
    {
        "name": "postconsulta_seguimiento",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 😊 ¿Cómo te sientes después de tu consulta de *{{2}}* con *{{3}}*?\n\n"
                    "Tu opinión nos ayuda a mejorar 🙏"
                ),
                "example": {
                    "body_text": [["Maria", "Traumatología", "Dr. Claudio Barraza"]]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Mejor"},
                    {"type": "QUICK_REPLY", "text": "Igual"},
                    {"type": "QUICK_REPLY", "text": "Peor"},
                ],
            },
        ],
    },

    # 4. Reactivación de pacientes inactivos (MARKETING)
    {
        "name": "reactivacion_paciente",
        "language": "es",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 👋 Hace un tiempo no te vemos en el *Centro Médico Carampangue* 🏥\n\n"
                    "¿Quieres retomar tu atención de *{{2}}*? Puedo ayudarte a agendar ahora mismo."
                ),
                "example": {
                    "body_text": [["Pedro", "Kinesiología"]]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Sí, agendar"},
                    {"type": "QUICK_REPLY", "text": "No, gracias"},
                ],
            },
            {
                "type": "FOOTER",
                "text": "Responde STOP para no recibir más mensajes",
            },
        ],
    },

    # 5. Adherencia kinesiología
    {
        "name": "adherencia_kine",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 💪 Para que tu tratamiento de kinesiología funcione bien, "
                    "es importante mantener continuidad en las sesiones.\n\n"
                    "¿Quieres que te ayude a agendar la próxima?"
                ),
                "example": {
                    "body_text": [["Juan"]]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Sí, agendar"},
                    {"type": "QUICK_REPLY", "text": "Más adelante"},
                ],
            },
        ],
    },

    # 6. Control por especialidad
    {
        "name": "control_especialidad",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 😊 Ya va correspondiendo tu control de *{{2}}* 📅\n\n"
                    "Hacer el seguimiento a tiempo hace la diferencia. "
                    "¿Quieres ver horarios disponibles?"
                ),
                "example": {
                    "body_text": [["Ana", "Nutrición"]]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Sí, ver horarios"},
                    {"type": "QUICK_REPLY", "text": "No por ahora"},
                ],
            },
        ],
    },

    # 7. Cross-sell kinesiología (MARKETING)
    {
        "name": "crosssell_kine",
        "language": "es",
        "category": "MARKETING",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 😊 Muchas veces, tras una consulta de medicina o traumatología "
                    "se recomienda continuar con kinesiología para avanzar mejor.\n\n"
                    "¿Te gustaría agendar con nuestros kinesiólogos?"
                ),
                "example": {
                    "body_text": [["Carlos"]]
                },
            },
            {
                "type": "BUTTONS",
                "buttons": [
                    {"type": "QUICK_REPLY", "text": "Sí, me interesa"},
                    {"type": "QUICK_REPLY", "text": "No por ahora"},
                ],
            },
            {
                "type": "FOOTER",
                "text": "Responde STOP para no recibir más mensajes",
            },
        ],
    },

    # 8. Lista de espera — cupo disponible
    {
        "name": "lista_espera_cupo",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 👋\n\n"
                    "¡Buenas noticias! Se liberó un cupo para *{{2}}*.\n\n"
                    "📅 Primera hora disponible: *{{3}} a las {{4}}*\n\n"
                    "Si quieres agendarla escribe *menu* y te ayudo al tiro 😊\n\n"
                    "_Te escribimos porque estás en nuestra lista de espera._"
                ),
                "example": {
                    "body_text": [["Pedro", "Traumatología", "2026-04-15", "10:30"]]
                },
            },
        ],
    },

    # 9. Sistema recuperado (para pacientes en cola)
    {
        "name": "sistema_recuperado",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "✅ ¡Buenas noticias! Nuestro sistema de citas ya está operativo de nuevo 🎉\n\n"
                    "Si quieres retomar lo que estabas haciendo, escribe *menu* y te ayudo al tiro.\n\n"
                    "_Gracias por tu paciencia._"
                ),
            },
        ],
    },

    # 10. Alerta técnica a recepción
    {
        "name": "alerta_tecnica_admin",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "⚠️ *Alerta técnica CMC bot*\n\n"
                    "Medilink no responde desde las {{1}}.\n"
                    "Pacientes esperando: *{{2}}*\n\n"
                    "El bot avisó a cada paciente y les pedirá escribir cuando el sistema esté operativo."
                ),
                "example": {
                    "body_text": [["14:30 UTC", "3"]]
                },
            },
        ],
    },

    # 11. Medilink recuperado (aviso a recepción)
    {
        "name": "sistema_recuperado_admin",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "✅ *Medilink recuperado*\n\n"
                    "El bot ya está operativo. Se avisó a {{1}} paciente(s) que estaban esperando."
                ),
                "example": {
                    "body_text": [["3"]]
                },
            },
        ],
    },

    # 12. Informe/resultado listo para retirar
    {
        "name": "informe_listo",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 👋 Tu informe de *{{2}}* ya está disponible.\n\n"
                    "Responde a este mensaje y te lo enviamos por aquí 📄"
                ),
                "example": {
                    "body_text": [["Sergio", "Ecografía"]]
                },
            },
        ],
    },

    # 13. Seguimiento médico personalizado (doctor quiere saber cómo está el paciente)
    {
        "name": "seguimiento_medico",
        "language": "es",
        "category": "UTILITY",
        "components": [
            {
                "type": "BODY",
                "text": (
                    "Hola {{1}} 👋 El *{{2}}* del Centro Médico Carampangue quiere saber "
                    "cómo has evolucionado desde tu última consulta.\n\n"
                    "¿Cómo te has sentido? ¿Algún síntoma nuevo o cambio?\n\n"
                    "Responde a este mensaje y te orientamos 🙏"
                ),
                "example": {
                    "body_text": [["Sergio", "Dr. Rodrigo Olavarría"]]
                },
            },
        ],
    },
]


# ──────────────────────────────────────────────────────────────────────────────
# Registro
# ──────────────────────────────────────────────────────────────────────────────

def register_template(tpl: dict) -> dict:
    """Registra un template en Meta. Retorna la respuesta JSON."""
    payload = {
        "name": tpl["name"],
        "language": tpl["language"],
        "category": tpl["category"],
        "components": tpl["components"],
    }
    try:
        r = httpx.post(API_URL, headers=HEADERS, json=payload, timeout=30)
        return {"name": tpl["name"], "status": r.status_code, "response": r.json()}
    except httpx.TimeoutException:
        return {"name": tpl["name"], "status": 0, "response": {"error": {"message": "Timeout"}}}


def main():
    print(f"Registrando {len(TEMPLATES)} templates en WABA {WABA_ID}...\n")

    ok = 0
    fail = 0
    for tpl in TEMPLATES:
        result = register_template(tpl)
        status = result["status"]
        name = result["name"]
        resp = result["response"]

        if status == 200:
            tid = resp.get("id", "?")
            print(f"  ✅ {name} → id={tid} (status={resp.get('status', '?')})")
            ok += 1
        else:
            err = resp.get("error", {}).get("message", json.dumps(resp)[:120])
            print(f"  ❌ {name} → HTTP {status}: {err}")
            fail += 1

    print(f"\nResultado: {ok} registrados, {fail} fallidos")
    if fail:
        print("Los templates fallidos pueden requerir ajustes en el texto o ya estar registrados.")
        print("Si ya existen, elimínalos primero desde Meta Business Manager antes de re-registrar.")


if __name__ == "__main__":
    main()
