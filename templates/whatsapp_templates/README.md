# Templates de WhatsApp — Centro Médico Carampangue

Templates JSON listos para submit a Meta Business API (`POST /v19.0/{WABA_ID}/message_templates`).

## Archivos en esta carpeta

### Ya aprobados (no re-subir)
| Archivo | Nombre | Categoria |
|---|---|---|
| `consent_marketing_v1.json` | consent_marketing_v1 | MARKETING |
| `winback_generico_sensible_v1.json` | winback_generico_sensible_v1 | MARKETING |
| `winback_kinesiologia_v1.json` | winback_kinesiologia_v1 | MARKETING |
| `winback_medicina_general_v1.json` | winback_medicina_general_v1 | MARKETING |
| `winback_odontologia_v1.json` | winback_odontologia_v1 | MARKETING |
| `winback_one_shot_general_v1.json` | winback_one_shot_general_v1 | MARKETING |
| `winback_otorrino_v1.json` | winback_otorrino_v1 | MARKETING |

### Pendientes de aprobacion (estos hay que subir)
| Archivo | Nombre | Categoria | Variables | Flujo que lo usa |
|---|---|---|---|---|
| `crosssell_orl_fono.json` | crosssell_orl_fono | MARKETING | `{{1}}` nombre | `enviar_crosssell_orl_fono` (ORL → Fono) |
| `crosssell_fono_orl.json` | crosssell_fono_orl | MARKETING | `{{1}}` nombre | `enviar_crosssell_orl_fono` (Fono → ORL) |
| `crosssell_odonto_estetica.json` | crosssell_odonto_estetica | MARKETING | `{{1}}` nombre | `enviar_crosssell_odonto_estetica` |
| `crosssell_mg_chequeo.json` | crosssell_mg_chequeo | MARKETING | `{{1}}` nombre | `enviar_crosssell_mg_chequeo` |
| `cumpleanos.json` | cumpleanos | MARKETING | `{{1}}` nombre, `{{2}}` tip edad | `enviar_cumpleanos` |
| `winback_fidelizacion.json` | winback_fidelizacion | MARKETING | `{{1}}` nombre | `enviar_winback` en `fidelizacion.py` |
| `winback_dx_cronico.json` | winback_dx_cronico | MARKETING | `{{1}}` nombre, `{{2}}` patologia | `enviar_winback` (rama dx_tags) |
| `crosssell_dx_dm2.json` | crosssell_dx_dm2 | MARKETING | `{{1}}` nombre | `enviar_crosssell_dx` (dm2) |
| `crosssell_dx_hta.json` | crosssell_dx_hta | MARKETING | `{{1}}` nombre | `enviar_crosssell_dx` (hta) |
| `crosssell_dx_pap.json` | crosssell_dx_pap | MARKETING | `{{1}}` nombre | `enviar_crosssell_dx` (gineco/PAP) |
| `campana_invierno_influenza.json` | campana_invierno_influenza | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("invierno_influenza")` |
| `campana_invierno_respiratorio.json` | campana_invierno_respiratorio | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("invierno_respiratorio")` |
| `campana_vuelta_clases.json` | campana_vuelta_clases | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("vuelta_clases")` |
| `campana_mes_corazon.json` | campana_mes_corazon | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("mes_corazon")` |
| `campana_diabetes_noviembre.json` | campana_diabetes_noviembre | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("diabetes_noviembre")` |
| `campana_salud_mental.json` | campana_salud_mental | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("salud_mental")` |
| `campana_dental_marzo.json` | campana_dental_marzo | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("dental_marzo")` |
| `campana_mujer_octubre.json` | campana_mujer_octubre | MARKETING | `{{1}}` nombre | `enviar_campana_estacional("mujer_octubre")` |

---

## Opcion A — Subir via script (recomendado)

### Dry-run (ver que se enviaria sin tocar nada)
```bash
cd /opt/chatbot-cmc
python scripts/upload_templates_to_meta.py
```

### Subir todos los pendientes
```bash
python scripts/upload_templates_to_meta.py --apply
```

### Subir un template especifico
```bash
python scripts/upload_templates_to_meta.py --apply --file crosssell_dx_dm2.json
```

El script omite automaticamente los ya aprobados (lista `ALREADY_APPROVED` en el script).
Usa `--include-approved` solo si necesitas forzar un re-submit.

**Variables de entorno requeridas** (deben estar en `.env`):
- `META_ACCESS_TOKEN` — token permanente del System User `Chatbotcmc-systemuser`
- `WHATSAPP_BUSINESS_ACCOUNT_ID` — WABA ID (no es el Phone Number ID)

---

## Opcion B — cURL manual (un template a la vez)

```bash
# Reemplazar WABA_ID y ACCESS_TOKEN con los valores reales
WABA_ID="TU_WABA_ID"
ACCESS_TOKEN="TU_ACCESS_TOKEN"
TEMPLATE_FILE="templates/whatsapp_templates/crosssell_dx_dm2.json"

curl -s -X POST \
  "https://graph.facebook.com/v19.0/${WABA_ID}/message_templates" \
  -H "Authorization: Bearer ${ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d @"${TEMPLATE_FILE}" | python3 -m json.tool
```

Respuesta exitosa:
```json
{
  "id": "123456789",
  "status": "PENDING",
  "category": "MARKETING"
}
```

---

## Opcion C — Meta Business Manager (UI)

1. Ir a https://business.facebook.com → WhatsApp Manager → Plantillas de mensajes
2. Clic en "Crear plantilla"
3. Copiar manualmente los valores de cada JSON:
   - Nombre (campo `name`)
   - Idioma: Espanol (Chile) (`es_CL`)
   - Categoria: Marketing
   - Cuerpo del mensaje (campo `components[].text` del BODY)
   - Variables: usar los valores del campo `example.body_text`
   - Botones: agregar cada entrada de `buttons`
4. Enviar para revision (tipicamente 24h habiles)

---

## Estados posibles de un template

| Estado | Descripcion |
|---|---|
| `PENDING` | En revision por Meta (24-48h habiles) |
| `APPROVED` | Listo para usar fuera de la ventana 24h |
| `REJECTED` | Rechazado — ver motivo en Business Manager |
| `PAUSED` | Pausado por baja calidad (muchos reportes) |
| `DISABLED` | Deshabilitado por Meta |

---

## Notas importantes

- **WABA ID vs Phone Number ID**: son distintos. El WABA ID está en Meta Business Suite → Configuracion de cuenta de WhatsApp. El Phone Number ID esta en el dashboard de la app.
- **Tiempo de aprobacion**: MARKETING puede tardar hasta 48h. UTILITY suele aprobarse en minutos.
- **Rechazos frecuentes**: lenguaje excesivamente promocional, mencionar precios sin ser UTILITY, templates identicos a uno existente con otro nombre.
- **Limite de templates**: el free tier tiene tope de templates activos. Con el payment method activo (USD 20 cargados en abril 2026) no deberia haber restriccion.
- **Cooldown de campanas**: los cooldowns estan en `fidelizacion.py` (`puede_enviar_campana`). Los templates aprobados no eximen del cooldown logico del bot.

---

## Borradores sin subir (sufijo `.DRAFT.json`, no confundir con aprobados)

| Archivo | Nombre | Categoria propuesta | Para que |
|---|---|---|---|
| `seguimiento_consulta_pendiente.DRAFT.json` | seguimiento_consulta_pendiente | UTILITY | Carril de persistencia (`app/persistencia.py`) — segundo toque a alguien que dejó una consulta de agendamiento a medias, cuando la ventana de 24h ya cerró. Hoy esa rama del código está INERTE (no envía nada) hasta que este template esté APPROVED. Quitar los campos `_nota`/`_justificacion_utility`/`_flujo_que_la_usaria` antes de hacer el submit a Meta (son solo documentación interna, no son parte del schema).
