/* Portal del paciente (portal_app) — contenido educativo.
   Fuente: templates/portal_v5.html (GUIDES, REDFLAGS, TIPS_CATALOG, PROMO_CATALOG)
   y portal_v4 (famSugerencias). NO se agregó contenido clínico nuevo: solo se
   cambiaron etiquetas con siglas ("HTA", "DM2") por palabras y se quitaron
   precios fijos (el precio vigente se lee del catálogo en vivo).
   Se carga a pedido (lazy) cuando la persona abre Mi salud, Familia o Centro. */
window.CMC_SALUD = (function(){
'use strict';
const GUIDES = [
  { id:'presion', cat:'Técnica', sub:'5 minutos', cond:['hta','hiperten','presi'],
    title:'Cómo tomar su presión en casa',
    steps:['<strong>Repose 5 minutos</strong> sentado. Espalda apoyada, pies en el suelo.',
           'Apoye el brazo en una mesa, a la altura del corazón. <strong>Ponga el manguito (la banda) sobre la piel desnuda</strong>, 2 dedos por sobre el pliegue del codo.',
           'No hable ni cruce las piernas mientras mide.',
           'Tome 2 mediciones, con 1 minuto entre una y otra. Si su aparato tiene modo "promedio", úselo.',
           'Anote el <strong>promedio</strong> en su portal. Ideal: en la mañana en ayunas (antes de comer) y en la noche antes de dormir.',
           'Avise a su médico si marca 180 o más (número alto) o 120 o más (número bajo). Eso es una crisis.'] },
  { id:'glicemia', cat:'Técnica', sub:'3 minutos', cond:['dm2','diab'],
    title:'Cómo medir su glicemia (azúcar en la sangre)',
    steps:['Lave sus manos con agua tibia y jabón. Séquelas bien.',
           'Ponga la tira en el aparato (glucómetro). Enciéndalo si es necesario.',
           'Use el <strong>costado del dedo</strong> (no la yema). Cambie de dedo cada vez para no lastimarlos.',
           'Pinche con la lanceta. Ponga la gota sobre la tira, sin untarla.',
           'Anote el valor y el momento: <strong>en ayunas</strong> (8 horas o más sin comer) o <strong>2 horas después de comer</strong>.',
           'Meta habitual en diabetes tipo 2: en ayunas entre 70 y 130; después de comer, menos de 180. Su médico puede ajustarla.'] },
  { id:'temp', cat:'Técnica', sub:'2 minutos', cond:[],
    title:'Cómo tomar la temperatura',
    steps:['Si usa termómetro de axila, seque bien la axila antes de medir.',
           'Ponga el sensor al centro de la axila. Mantenga el brazo pegado al cuerpo 3 a 5 minutos (digital) o hasta que suene.',
           'Normal: 36 a 37,4 °C en la axila. En la boca sume 0,3 a 0,5; en el recto sume 0,5 a 1.',
           '<strong>Fiebre</strong>: 38 °C o más en la axila, o su equivalente. Entre 37,5 y 37,9 es fiebre baja (febrícula).',
           'Mida antes de tomar remedios para la fiebre (paracetamol o ibuprofeno). Anote la hora.'] },
  { id:'inhalador', cat:'Remedios', sub:'1 minuto', cond:['asma','epoc'],
    title:'Cómo usar su inhalador (salbutamol)',
    steps:['Agite bien el inhalador antes de usarlo.',
           'Use siempre <strong>aerocámara</strong> (el tubo que se conecta al inhalador) si tiene. Ayuda a que el remedio llegue a los pulmones, sobre todo en niños y adultos mayores.',
           'Bote el aire despacio antes de inhalar.',
           'Apriete el inhalador y, al mismo tiempo, <strong>tome aire profundo y lento</strong>. Aguante el aire 10 segundos.',
           'Espere 30 segundos entre una aplicación y otra.',
           'Si su inhalador es de uso diario (corticoides), enjuáguese la boca con agua después de usarlo.'] },
  { id:'insulina', cat:'Remedios', sub:'3 minutos', cond:['dm2','insulin'],
    title:'Cómo ponerse la insulina',
    steps:['Guarde la insulina en el refrigerador (2 a 8 °C). El lápiz que está usando puede quedar a temperatura ambiente hasta 30 días.',
           'Lave sus manos. Revise la insulina: la rápida debe verse transparente; la NPH, bien mezclada.',
           'Ponga una aguja nueva. Bote 2 unidades al aire para sacar las burbujas.',
           'Inyecte bajo la piel (no en el músculo): <strong>abdomen</strong> (actúa más rápido), muslo o brazo.',
           'Cambie el punto de inyección cada día, a 2 cm del anterior. Así evita que la piel se endurezca o forme bultos (lipodistrofia).',
           'Deje la aguja 10 segundos bajo la piel antes de sacarla.'] },
  { id:'heridas', cat:'Cuidado', sub:'5 minutos', cond:[],
    title:'Lavado básico de heridas',
    steps:['Lave sus manos antes. Use guantes si tiene.',
           'Lave la herida con <strong>suero fisiológico</strong> o con agua potable, a chorro suave.',
           'Seque con una gasa limpia, tocando con suavidad, sin arrastrar.',
           'Si hay riesgo de infección, limpie con un desinfectante como la povidona.',
           'Cubra con gasa y vendaje. Cambie la curación al menos 1 vez al día, o si se moja.',
           'Consulte si ve: pus, fiebre, la zona roja cada vez más grande, o una herida que no cierra en 7 días.'] },
];

/* Banderas rojas: educan CUÁNDO pedir ayuda. Nunca indican dosis ni diagnostican. */
const REDFLAGS = [
  { sev:'urgente', cond:[], title:'Dolor de pecho',
    trigger:'Dolor o presión en el pecho. Peor si baja al brazo, cuello o mandíbula, con sudor frío o falta de aire.',
    action:'No espere. No maneje usted. Llame de inmediato.' },
  { sev:'urgente', cond:[], title:'Señales de ataque cerebral',
    trigger:'La boca se tuerce, un brazo pierde fuerza, o cuesta hablar o entender. Aparece de golpe.',
    action:'Anote la hora en que empezó y llame ya. Cada minuto cuenta.' },
  { sev:'urgente', cond:[], title:'Falta de aire repentina',
    trigger:'Le falta el aire de repente, estando en reposo, o los labios o las uñas se ponen morados.',
    action:'Es una emergencia respiratoria.' },
  { sev:'pronto', cond:[], title:'Fiebre que no cede',
    trigger:'Fiebre de 38 °C o más que no baja en 3 días, o de 39 °C con mucho decaimiento.',
    action:'Consulte el mismo día. Escríbanos por WhatsApp para pedir una hora.' },
  { sev:'urgente', cond:['hta','hiperten','presi'], title:'Crisis de presión alta',
    trigger:'Presión de 180/120 o más, junto con dolor de cabeza fuerte, visión borrosa, dolor de pecho o falta de aire.',
    action:'Acuda a urgencia o llame al SAMU.' },
  { sev:'pronto', cond:['hta','hiperten','presi'], title:'Presión alta sostenida',
    trigger:'Presión sobre 160/100 varios días seguidos, aunque no tenga síntomas.',
    action:'Pida control con su médico esta semana. Registre sus valores en el portal.' },
  { sev:'urgente', cond:['dm2','diab'], title:'Azúcar muy alta',
    trigger:'Azúcar (glicemia) sobre 300, con mucha sed, ganas de orinar a cada rato, náuseas, aliento con olor dulce o respiración agitada.',
    action:'Su diabetes puede estar descompensada (fuera de control). Es grave. Acuda a urgencia.' },
  { sev:'urgente', cond:['dm2','diab','insulin'], title:'Azúcar muy baja',
    trigger:'Temblor, sudor frío, confusión o mareo. Es más frecuente si comió poco.',
    action:'Tome algo dulce YA: jugo, bebida con azúcar o caramelos. Si no mejora en 15 minutos, repita y pida ayuda.' },
  { sev:'pronto', cond:['dm2','diab'], title:'Herida en el pie',
    trigger:'Una herida en el pie que no cierra, cambia de color, huele mal o no le duele.',
    action:'No espere: puede ser pie diabético. Pida hora pronto.' },
  { sev:'urgente', cond:['asma','epoc'], title:'Crisis respiratoria',
    trigger:'El salbutamol no le alivia tras varias aplicaciones, no puede terminar una frase, o los labios se ponen morados.',
    action:'Es una crisis. Llame al SAMU ahora.' },
  { sev:'pronto', cond:['asma','epoc'], title:'Asma o enfermedad pulmonar descontrolada',
    trigger:'Despierta en la noche ahogado o tosiendo, o usa el inhalador mucho más que de costumbre.',
    action:'Su tratamiento puede necesitar un ajuste. Pida control.' },
];

const TIPS = {
  sal:{cat:'Alimentación', title:'Reduzca la sal', desc:'Menos de 5 gramos al día. Evite embutidos, sopas en sobre y snacks salados.'},
  azucar:{cat:'Alimentación', title:'Controle el azúcar', desc:'Limite las bebidas azucaradas y los postres. Prefiera la fruta entera.'},
  frutas:{cat:'Alimentación', title:'5 porciones al día', desc:'Coma frutas y verduras en cada comida. Varíe los colores.'},
  agua:{cat:'Hidratación', title:'8 vasos de agua', desc:'Tome agua durante el día. Más si hace calor, hace ejercicio o toma diuréticos (remedios que hacen orinar).'},
  caminar:{cat:'Actividad física', title:'Camine 30 minutos al día', desc:'5 días a la semana. Ayuda a bajar la presión y el azúcar.'},
  fuerza:{cat:'Actividad física', title:'Fuerza 2 veces por semana', desc:'Use pesas, bandas elásticas o su propio peso. Protege huesos y músculos.'},
  sueno:{cat:'Sueño', title:'7 a 8 horas de sueño', desc:'Acuéstese y levántese a la misma hora. Apague las pantallas 30 minutos antes.'},
  tabaco:{cat:'Salud general', title:'Si fuma, déjelo', desc:'En 12 meses, su riesgo de enfermar del corazón baja a la mitad.'},
  alcohol:{cat:'Salud general', title:'Alcohol con moderación', desc:'Máximo 1 trago al día en mujeres y 2 en hombres. Deje días sin alcohol.'},
  mental:{cat:'Salud mental', title:'Cuide su salud mental', desc:'Tome pausas, vea a sus cercanos, tenga pasatiempos. ¿Más de 2 semanas triste? Consulte.'},
  presion_medir:{cat:'Presión alta', title:'Mida su presión', desc:'1 a 2 veces por semana en casa, sentado y tranquilo.'},
  glicemia_tip:{cat:'Diabetes', title:'Mida su glicemia', desc:'En ayunas y 2 horas después de comer. Menos de 180 después de comer es buena meta.'},
  pies:{cat:'Diabetes', title:'Revise sus pies', desc:'Míreselos todos los días. ¿Heridas o callos? Pida hora con podología.'},
};

/* Servicios del centro (de PROMO_CATALOG v5). `esp` = especialidad del catálogo
   en vivo, de donde sale el precio vigente. Sin precios fijos aquí. */
const SERVICIOS = {
  chequeo:{esp:'Medicina General', title:'Chequeo médico general', desc:'Presión, exámenes básicos y un plan para cuidarse.'},
  kine:{esp:'Kinesiología', title:'Kinesiología', desc:'Atención con bono Fonasa. Para dolores, lesiones y recuperar movilidad.'},
  cardio:{esp:'Cardiología', title:'Control del corazón', desc:'Con el cardiólogo. Recomendado si tiene presión alta o diabetes.'},
  nutri:{esp:'Nutrición', title:'Plan de alimentación', desc:'Con nutricionista. Para diabetes, presión alta y bajar de peso.'},
  psico:{esp:'Psicología Adulto', title:'Atención psicológica', desc:'Sesiones de 45 minutos.'},
  odonto:{esp:'Odontología General', title:'Dentista', desc:'La evaluación incluye una revisión general y un plan.'},
  eco:{esp:'Ecografía', title:'Ecografías', desc:'Abdomen, riñones, tiroides y doppler venoso (examen de las venas).'},
  podologia:{esp:'Podología', title:'Podología', desc:'Uñas encarnadas, callos y cuidado del pie diabético.'},
  matrona:{esp:'Matrona', title:'Control con matrona y PAP', desc:'El PAP es el examen que detecta a tiempo el cáncer de cuello uterino.'},
  estetica:{esp:'Estética Facial', title:'Estética facial', desc:'Evaluación para cuidar y rejuvenecer la piel.'},
};

function dxStr(d){ return (d||[]).join(' | ').toLowerCase(); }
function guides(d){ const s=dxStr(d); const rel=GUIDES.filter(g=>g.cond.length&&g.cond.some(k=>s.includes(k))); return rel.concat(GUIDES.filter(g=>!rel.includes(g))); }
function flags(d){ const s=dxStr(d); return REDFLAGS.filter(f=>!f.cond.length||f.cond.some(k=>s.includes(k))).sort((a,b)=>(a.sev==='urgente'?0:1)-(b.sev==='urgente'?0:1)); }
function tips(d){
  const s=dxStr(d), has=k=>s.includes(k), picks=[];
  if (has('hta')||has('hiperten')||has('presi')) picks.push('sal','presion_medir','caminar');
  if (has('dm2')||has('diab')) picks.push('azucar','glicemia_tip','pies','caminar');
  if (has('asma')||has('epoc')) picks.push('tabaco');
  if (has('ansie')||has('depre')) picks.push('mental','sueno');
  const out=[], seen=new Set();
  for (const k of picks.concat(['caminar','frutas','agua','sueno','fuerza','mental','tabaco','alcohol'])) { if(TIPS[k]&&!seen.has(k)){ seen.add(k); out.push(TIPS[k]); } if(out.length>=8) break; }
  return out;
}
function servicios(data){
  const s=dxStr(data.diagnosticos);
  const specs=(data.historial||[]).map(c=>(c.especialidad||'').toLowerCase());
  const covered=specs.concat((data.citas_futuras||[]).map(c=>(c.especialidad||'').toLowerCase()));
  const picks=[];
  if (s.includes('hta')||s.includes('hiperten')) picks.push('cardio','nutri');
  if (s.includes('dm2')||s.includes('diab')) picks.push('nutri','podologia','cardio','eco');
  if (specs.some(x=>x.includes('traumat'))) picks.push('kine');
  if (specs.some(x=>x.includes('odontolog'))) picks.push('odonto');
  if (specs.some(x=>x.includes('medicina general'))) picks.push('nutri','psico','eco');
  if (!(data.citas_futuras||[]).length) picks.push('chequeo');
  const out=[], seen=new Set();
  for (const k of picks.concat(['chequeo','nutri','psico','kine','odonto','eco','matrona','estetica','cardio','podologia'])) {
    const sv=SERVICIOS[k]; if(!sv||seen.has(k)) continue;
    if (k!=='chequeo' && covered.some(c=>c.includes(sv.esp.toLowerCase().slice(0,6)))) continue;
    seen.add(k); out.push(sv); if(out.length>=6) break;
  }
  return out;
}
/* Sugerencias por edad para cada integrante (de famSugerencias v4/v5, sin precios). */
function sugerencias(m){
  if (m.edad==null) return [];
  const out=[], esp=((m.proxima&&m.proxima.especialidad)||'').toLowerCase(), mujer=String(m.sexo||'').toUpperCase().startsWith('F');
  if (m.edad<=14){ if(!/odont/.test(esp)) out.push({t:'Control dental infantil', esp:'Odontología General'}); out.push({t:'Control sano', esp:'Medicina General'}); }
  else if (m.edad<60){ out.push({t:'Chequeo preventivo anual', esp:'Medicina General'}); if(mujer&&m.edad>=25) out.push({t:'PAP al día', esp:'Matrona'}); else if(!/odont/.test(esp)) out.push({t:'Control dental', esp:'Odontología General'}); }
  else { if(!/podo/.test(esp)) out.push({t:'Podología preventiva', esp:'Podología'}); if(!/kine/.test(esp)) out.push({t:'Kinesiología para fuerza y equilibrio', esp:'Kinesiología'}); if(!/cardio/.test(esp)) out.push({t:'Control del corazón', esp:'Cardiología'}); }
  return out.slice(0,2);
}
return {guides, flags, tips, servicios, sugerencias};
})();
