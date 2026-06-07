"""Test e2e winback — standalone. Ejecutar desde /opt/chatbot-cmc/
con: cd /opt/chatbot-cmc && source venv/bin/activate && python tests/test_winback_e2e.py [test_num]
Usa la BD real de producción y Meta API real. Solo envía a +56987834148.
"""
import asyncio
import os
import sys
import time
import logging

# Añadir app al path
sys.path.insert(0, '/opt/chatbot-cmc/app')
# Cargar .env manualmente
from pathlib import Path
env_path = Path('/opt/chatbot-cmc/.env')
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith('#') and '=' in line:
        k, v = line.split('=', 1)
        os.environ.setdefault(k.strip(), v.strip())

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('test_winback_e2e')

RODRIGO_PHONE = '56987834148'  # sin +
RODRIGO_PHONE_PLUS = '+56987834148'

# ─── Colores para output ─────────────────────────────────────────────────────
GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
RESET  = '\033[0m'

def PASS(msg): print(f'{GREEN}PASS{RESET} {msg}')
def FAIL(msg): print(f'{RED}FAIL{RESET} {msg}')
def WARN(msg): print(f'{YELLOW}WARN{RESET} {msg}')
def INFO(msg): print(f'     {msg}')

results = []

async def test_1_consent_template():
    """TEST 1 — Envío real de consent_marketing_v1 a Rodrigo."""
    print('\n=== TEST 1: Consent UTILITY base ===')
    from messaging import send_whatsapp_template
    from winback import is_template_approved

    # 1a. Verificar que template está APPROVED
    approved = await is_template_approved('consent_marketing_v1')
    if approved:
        PASS('1a. consent_marketing_v1 está APPROVED en Meta')
    else:
        FAIL('1a. consent_marketing_v1 NO está APPROVED en Meta — abortando TEST 1')
        results.append(('TEST 1', 'FAIL', 'consent_marketing_v1 no APPROVED'))
        return

    # 1b. Enviar template real
    try:
        await send_whatsapp_template(
            to=RODRIGO_PHONE,
            template_name='consent_marketing_v1',
            body_params=['Rodrigo'],
        )
        PASS('1b. send_whatsapp_template retornó sin excepción')
        INFO('  → Verificar manualmente en WhatsApp +56987834148:')
        INFO('    - Mensaje con nombre Rodrigo rellenado')
        INFO('    - 2 botones: [Si, acepto] [No, gracias]')
        INFO('    - Footer sobre no enviar más promociones si responde NO')
        results.append(('TEST 1', 'PASS', 'consent enviado a Rodrigo'))
    except Exception as e:
        FAIL(f'1b. send_whatsapp_template lanzó excepción: {e}')
        results.append(('TEST 1', 'FAIL', str(e)))


def test_2_reconocimiento_respuestas():
    """TEST 2 — Verifica lógica de detección de SI/NO sin llamar API.
    Simula la lógica de flows.py lines 2307-2316.
    """
    print('\n=== TEST 2: Reconocimiento de respuestas (lógica offline) ===')

    def normalize(txt: str) -> str:
        """Simula normalizar_texto_paciente: minúscula + sin tildes (básico)."""
        import unicodedata
        s = txt.lower()
        # Quitar tildes (excepto ñ)
        out = []
        for c in unicodedata.normalize('NFD', s):
            if unicodedata.category(c) == 'Mn':
                continue
            out.append(c)
        return ''.join(out)

    casos = [
        # (input_raw, expected_consent_si, expected_consent_no, descripcion)
        ('SI',             True,  False, '2.1 SI mayúsculas'),
        ('Si',             True,  False, '2.2 Si mixto'),
        ('sí',             True,  False, '2.3 sí con tilde'),
        ('si acepto',      True,  False, '2.4 si acepto frase'),
        ('claro',          False, False, '2.5 claro — NO debe ser consent si'),
        ('ok',             False, False, '2.6 ok — NO debe ser consent si'),
        ('NO',             False, True,  '2.7 NO mayúsculas'),
        ('no gracias',     False, True,  '2.8 no gracias'),
        ('no quiero',      False, False, '2.9 no quiero — solo NO puro para consent'),
        ('baja',           False, False, '2.10 baja — es opt-out, no consent_no'),
        ('BAJA',           False, False, '2.11 BAJA — es opt-out, no consent_no'),
        ('Sí, acepto',     True,  False, '2.12 texto exacto del botón SI'),
        ('No, gracias',    False, True,  '2.13 texto exacto del botón NO'),
        ('hola',           False, False, '2.14 hola — no debe gatillar consent'),
    ]

    all_ok = True
    for raw, exp_si, exp_no, desc in casos:
        tl = raw.lower()
        tl_norm = normalize(raw)

        # Lógica exacta de flows.py líneas 2307-2316
        _es_consent_si = (
            tl in ('sí, acepto', 'si, acepto', 'si acepto', 'si')
            or tl_norm in ('si, acepto', 'si acepto', 'si')
            or raw in ('Sí, acepto', 'Si, acepto')
        )
        _es_consent_no = (
            tl in ('no, gracias', 'no gracias', 'no')
            or tl_norm in ('no, gracias', 'no gracias', 'no')
            or raw in ('No, gracias', 'No gracias')
        )

        ok = (_es_consent_si == exp_si and _es_consent_no == exp_no)
        if ok:
            PASS(f'{desc}')
            INFO(f'  tl={tl!r} tl_norm={tl_norm!r} → si={_es_consent_si} no={_es_consent_no}')
        else:
            FAIL(f'{desc}')
            INFO(f'  tl={tl!r} tl_norm={tl_norm!r}')
            INFO(f'  esperado si={exp_si} no={exp_no}')
            INFO(f'  obtenido si={_es_consent_si} no={_es_consent_no}')
            all_ok = False

    results.append(('TEST 2', 'PASS' if all_ok else 'FAIL', 'detección SI/NO offline'))
    return all_ok


async def test_3_bd_consent_state():
    """TEST 3 — Verifica estado actual de Rodrigo en bi.marketing_consent."""
    print('\n=== TEST 3: Estado BD marketing_consent para Rodrigo ===')
    from winback import bi_conn

    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT phone, status, consent_sent_at, response_at, response_method
                       FROM bi.marketing_consent
                       WHERE phone = %s OR phone = %s""",
                    (RODRIGO_PHONE, RODRIGO_PHONE_PLUS)
                )
                rows = cur.fetchall()

        if rows:
            PASS('3a. Rodrigo tiene registro en bi.marketing_consent')
            for r in rows:
                INFO(f'  phone={r[0]} status={r[1]} sent_at={r[2]} response_at={r[3]} method={r[4]}')
            results.append(('TEST 3', 'PASS', f'status={rows[0][1]}'))
        else:
            WARN('3a. Rodrigo NO tiene registro en bi.marketing_consent (aún no respondió o no se envió)')
            INFO('  → Esto es esperado si TEST 1 se acaba de ejecutar y Rodrigo no respondió aún')
            results.append(('TEST 3', 'WARN', 'sin registro consent para Rodrigo'))
    except Exception as e:
        FAIL(f'3a. Error consultando BD: {e}')
        results.append(('TEST 3', 'FAIL', str(e)))


async def test_3b_candidato_bi():
    """TEST 3b — Verifica si Rodrigo está en v_winback_cohortes_contactables."""
    print('\n=== TEST 3b: Candidato BI para Rodrigo ===')
    from winback import get_candidato_por_phone

    try:
        candidato = get_candidato_por_phone(RODRIGO_PHONE)
        if candidato:
            PASS('3b. Rodrigo ESTÁ en v_winback_cohortes_contactables')
            INFO(f'  nombre={candidato.get("nombre")} especialidad={candidato.get("ultima_especialidad")}')
            INFO(f'  cohorte={candidato.get("cohorte")} dias_inactivo={candidato.get("dias_inactivo")}')
            INFO('  → Al aceptar consent, recibirá winback event-driven')
            results.append(('TEST 3b', 'PASS', f'candidato encontrado cohorte={candidato.get("cohorte")}'))
        else:
            WARN('3b. Rodrigo NO está en v_winback_cohortes_contactables')
            INFO('  → Al aceptar consent, path fallback: "Listo, queda registrado."')
            results.append(('TEST 3b', 'WARN', 'sin candidato en BI para Rodrigo'))
    except Exception as e:
        FAIL(f'3b. Error: {e}')
        results.append(('TEST 3b', 'FAIL', str(e)))


async def test_4_winback_templates():
    """TEST 4 — Envío directo de los 6 templates winback_v2 a Rodrigo."""
    print('\n=== TEST 4: Los 6 templates winback_v2 a Rodrigo ===')
    from messaging import send_whatsapp_template
    from winback import is_template_approved

    templates = [
        ('winback_medicina_general_v2', ['Rodrigo', 'el Dr. Olavarría'],     '4.1 winback_medicina_general_v2'),
        ('winback_odontologia_v2',      ['Rodrigo', 'la Dra. Burgos'],        '4.2 winback_odontologia_v2'),
        ('winback_kinesiologia_v2',     ['Rodrigo', 'el Kine Etcheverry'],    '4.3 winback_kinesiologia_v2'),
        ('winback_otorrino_v2',         ['Rodrigo'],                          '4.4 winback_otorrino_v2'),
        ('winback_generico_sensible_v2',['Rodrigo'],                          '4.5 winback_generico_sensible_v2'),
        ('winback_one_shot_general_v2', ['Rodrigo'],                          '4.6 winback_one_shot_general_v2'),
    ]

    for tpl_name, params, desc in templates:
        # a. Verificar approval primero
        approved = await is_template_approved(tpl_name)
        if not approved:
            FAIL(f'{desc} — NO APPROVED en Meta')
            results.append((desc, 'FAIL', 'template no APPROVED'))
            continue

        # b. Enviar
        try:
            await send_whatsapp_template(
                to=RODRIGO_PHONE,
                template_name=tpl_name,
                body_params=params,
            )
            PASS(f'{desc} — enviado OK (params={params})')
            INFO(f'  → Verificar en WA: nombre + profesional renderizados, botones, footer BAJA, tel (41)')
            results.append((desc, 'PASS', f'enviado params={params}'))
        except Exception as e:
            FAIL(f'{desc} — excepción: {e}')
            results.append((desc, 'FAIL', str(e)))

        # Pausa entre envíos para no saturar
        await asyncio.sleep(3)


async def test_5_edge_case_nombres():
    """TEST 5 — Edge cases de nombres en body_params."""
    print('\n=== TEST 5: Edge cases de nombres (solo winback_medicina_general_v2) ===')
    from messaging import send_whatsapp_template
    from winback import is_template_approved

    approved = await is_template_approved('winback_medicina_general_v2')
    if not approved:
        FAIL('5. winback_medicina_general_v2 no APPROVED — saltando TEST 5')
        results.append(('TEST 5', 'FAIL', 'template no approved'))
        return

    casos = [
        (['María José', 'el Dr. Olavarría'],  '5.1 nombre con espacio'),
        (['José',        'el Dr. Olavarría'],  '5.2 nombre con tilde'),
        (['Ñancupil',    'el Dr. Olavarría'],  '5.3 nombre con ñ'),
        ([''],                                 '5.4 nombre vacío'),
        # None no se puede pasar como body_param directamente → simular con str
        (['Juan Carlos Pérez', 'el Dr. Olavarría'], '5.6 nombre largo'),
        (['María de los Ángeles del Rosario', 'el Dr. Olavarría'], '5.7 nombre muy largo'),
    ]

    for params, desc in casos:
        try:
            await send_whatsapp_template(
                to=RODRIGO_PHONE,
                template_name='winback_medicina_general_v2',
                body_params=params,
            )
            PASS(f'{desc} — enviado sin excepción (params={params})')
            results.append((desc, 'PASS', f'params={params}'))
        except Exception as e:
            FAIL(f'{desc} — excepción: {e}')
            results.append((desc, 'FAIL', str(e)))
        await asyncio.sleep(2)

    # 5.5 None como body_param — esto no se puede enviar a Meta; verificar que falla limpio
    print('  --- 5.5 body_params=[None] ---')
    try:
        await send_whatsapp_template(
            to=RODRIGO_PHONE,
            template_name='winback_medicina_general_v2',
            body_params=[None],
        )
        WARN('5.5 body_params=[None] — Meta aceptó None sin excepción (verificar renderizado)')
        results.append(('5.5 None param', 'WARN', 'Meta aceptó None'))
    except Exception as e:
        PASS(f'5.5 body_params=[None] falló correctamente con: {type(e).__name__}: {e}')
        results.append(('5.5 None param', 'PASS', f'falló con {type(e).__name__}'))


async def test_6_gates_seguridad():
    """TEST 6 — Gates de seguridad."""
    print('\n=== TEST 6: Gates de seguridad ===')

    # 6.1 WINBACK_ACTIVE check en flows
    winback_env = os.getenv('WINBACK_ACTIVE', 'false').lower()
    winback_active = winback_env in ('true', '1', 'yes')
    INFO(f'6.1 WINBACK_ACTIVE={winback_env} (en .env del server)')
    if winback_active:
        PASS('6.1 WINBACK_ACTIVE=true — el handler consent SI disparará winback')
    else:
        WARN('6.1 WINBACK_ACTIVE=false — consent SI solo dará acuse genérico (sin winback)')
    results.append(('6.1 WINBACK_ACTIVE', 'PASS' if winback_active else 'WARN', f'WINBACK_ACTIVE={winback_env}'))

    # 6.2 ya_enviado_winback_hoy — rate limit doble envío
    from winback import ya_enviado_winback_hoy
    already = ya_enviado_winback_hoy(RODRIGO_PHONE)
    INFO(f'6.2 ya_enviado_winback_hoy(Rodrigo) = {already}')
    if already:
        WARN('6.2 Ya hay un winback de hoy para Rodrigo — el rate limit funciona (segundo envío sería bloqueado)')
    else:
        PASS('6.2 No hay winback de hoy para Rodrigo — rate limit no bloquea')
    results.append(('6.2 rate_limit_winback_hoy', 'PASS', f'ya_enviado={already}'))

    # 6.3 opt-out check
    from winback import phone_in_opt_out
    in_opt_out = phone_in_opt_out(RODRIGO_PHONE)
    if not in_opt_out:
        PASS('6.3 Rodrigo NO está en opt_outs_marketing — puede recibir winback')
    else:
        WARN('6.3 Rodrigo ESTÁ en opt_outs_marketing — winback bloqueado para él')
    results.append(('6.3 opt_out', 'PASS' if not in_opt_out else 'WARN', f'in_opt_out={in_opt_out}'))

    # 6.4 has_marketing_consent
    from winback import has_marketing_consent
    has_consent = has_marketing_consent(RODRIGO_PHONE)
    INFO(f'6.4 has_marketing_consent(Rodrigo) = {has_consent}')
    if has_consent:
        PASS('6.4 Rodrigo tiene marketing_consent=accepted — puede recibir winback')
    else:
        WARN('6.4 Rodrigo NO tiene marketing_consent=accepted — send_winback lo bloqueará')
    results.append(('6.4 marketing_consent', 'PASS' if has_consent else 'WARN', f'has_consent={has_consent}'))

    # 6.5 MARKETING_CONSENT_BLAST_ACTIVE debe ser false
    blast_env = os.getenv('MARKETING_CONSENT_BLAST_ACTIVE', 'false').lower()
    blast_active = blast_env in ('true', '1', 'yes')
    if not blast_active:
        PASS('6.5 MARKETING_CONSENT_BLAST_ACTIVE=false — blast pausado correctamente')
    else:
        FAIL('6.5 MARKETING_CONSENT_BLAST_ACTIVE=true — BLAST ACTIVO, no debería estarlo ahora')
    results.append(('6.5 blast_active', 'PASS' if not blast_active else 'FAIL', f'blast={blast_env}'))


async def test_8_bd_integridad():
    """TEST 8 — Integridad de BD: estado de tablas clave."""
    print('\n=== TEST 8: Integridad BD ===')
    from winback import bi_conn

    try:
        with bi_conn() as conn:
            with conn.cursor() as cur:
                # 8.1 marketing_consent para Rodrigo
                cur.execute(
                    "SELECT phone, status, consent_sent_at, response_at FROM bi.marketing_consent WHERE phone IN (%s,%s)",
                    (RODRIGO_PHONE, RODRIGO_PHONE_PLUS)
                )
                consent_rows = cur.fetchall()

                # 8.2 winback_envios para Rodrigo
                cur.execute(
                    "SELECT id, cohorte, template_meta, enviado_at, response_type FROM bi.winback_envios WHERE telefono IN (%s,%s) ORDER BY enviado_at DESC LIMIT 5",
                    (RODRIGO_PHONE, RODRIGO_PHONE_PLUS)
                )
                winback_rows = cur.fetchall()

                # 8.3 opt_outs_marketing para Rodrigo
                cur.execute(
                    "SELECT phone, source, opted_out_at FROM bi.opt_outs_marketing WHERE phone IN (%s,%s)",
                    (RODRIGO_PHONE, RODRIGO_PHONE_PLUS)
                )
                optout_rows = cur.fetchall()

        print('  8.1 bi.marketing_consent:')
        if consent_rows:
            for r in consent_rows:
                INFO(f'    phone={r[0]} status={r[1]} sent_at={r[2]} response_at={r[3]}')
            results.append(('8.1 marketing_consent', 'PASS', f'{len(consent_rows)} filas'))
        else:
            INFO('    (sin filas para Rodrigo)')
            results.append(('8.1 marketing_consent', 'WARN', 'sin filas para Rodrigo'))

        print('  8.2 bi.winback_envios:')
        if winback_rows:
            for r in winback_rows:
                INFO(f'    id={r[0]} cohorte={r[1]} template={r[2]} enviado={r[3]} resp={r[4]}')
            results.append(('8.2 winback_envios', 'PASS', f'{len(winback_rows)} filas recientes'))
        else:
            INFO('    (sin winback_envios para Rodrigo)')
            results.append(('8.2 winback_envios', 'WARN', 'sin filas — normal si no es candidato BI'))

        print('  8.3 bi.opt_outs_marketing:')
        if optout_rows:
            for r in optout_rows:
                INFO(f'    phone={r[0]} source={r[1]} opted_out_at={r[2]}')
            WARN('8.3 Rodrigo está en opt_outs — esto bloqueará winbacks futuros')
            results.append(('8.3 opt_outs', 'WARN', f'{len(optout_rows)} filas'))
        else:
            INFO('    (sin filas — OK)')
            PASS('8.3 Rodrigo NO está en opt_outs_marketing')
            results.append(('8.3 opt_outs', 'PASS', 'sin opt-out'))

    except Exception as e:
        FAIL(f'8. Error consultando BD: {e}')
        results.append(('TEST 8', 'FAIL', str(e)))


async def main():
    test_num = sys.argv[1] if len(sys.argv) > 1 else 'all'

    if test_num in ('all', '1'):
        await test_1_consent_template()
    if test_num in ('all', '2'):
        test_2_reconocimiento_respuestas()
    if test_num in ('all', '3'):
        await test_3_bd_consent_state()
        await test_3b_candidato_bi()
    if test_num in ('all', '6'):
        await test_6_gates_seguridad()
    if test_num in ('all', '4'):
        await test_4_winback_templates()
    if test_num in ('all', '5'):
        await test_5_edge_case_nombres()
    if test_num in ('all', '8'):
        await test_8_bd_integridad()

    print('\n' + '='*60)
    print('RESUMEN FINAL')
    print('='*60)
    fails  = [r for r in results if r[1] == 'FAIL']
    warns  = [r for r in results if r[1] == 'WARN']
    passes = [r for r in results if r[1] == 'PASS']
    print(f'  PASS: {len(passes)}  WARN: {len(warns)}  FAIL: {len(fails)}')
    if fails:
        print(f'{RED}  FALLOS:{RESET}')
        for r in fails:
            print(f'    {r[0]}: {r[2]}')
    if warns:
        print(f'{YELLOW}  WARNINGS:{RESET}')
        for r in warns:
            print(f'    {r[0]}: {r[2]}')
    return len(fails)


if __name__ == '__main__':
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
