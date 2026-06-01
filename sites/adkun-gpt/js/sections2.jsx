/* ADKUN — sections part 2: Enfoque, Manifiesto, Nosotros, Contacto, Footer. */

const STEPS = [
  { n: "01", kind: "understand", title: "Comprender", body: "Analizamos la operación y sus puntos críticos." },
  { n: "02", kind: "order",      title: "Ordenar",    body: "Definimos procesos, prioridades y conexiones." },
  { n: "03", kind: "design",     title: "Diseñar",    body: "Creamos experiencias digitales claras y simples." },
  { n: "04", kind: "scale",      title: "Escalar",    body: "Construimos plataformas preparadas para evolucionar." },
];

/* ---------------- ENFOQUE ---------------- */
function Enfoque() {
  return (
    <section id="enfoque" data-anchor className="relative bg-white py-24 lg:py-32">
      <div className="mx-auto max-w-[1240px] px-6 lg:px-10">
        <div className="max-w-[48rem]">
          <div className="reveal flex items-center gap-3 mb-6">
            <span className="h-px w-10 bg-copper"></span>
            <span className="font-sans text-[12px] font-semibold tracking-[0.28em] text-slate uppercase">Enfoque</span>
          </div>
          <h2 className="reveal reveal-d1 font-display font-bold text-navy tracking-tighter2 leading-[1.06] text-[clamp(1.9rem,4vw,3.1rem)]">
            Productos nacidos desde<br className="hidden md:block" /> problemas reales.
          </h2>
          <p className="reveal reveal-d2 mt-6 font-sans text-[1.1rem] leading-[1.65] text-slate max-w-[42rem]">
            En ADKUN no comenzamos por la tecnología. Comenzamos por comprender cómo funciona una operación, dónde aparecen las fricciones y qué decisiones podrían simplificarse. Luego construimos el producto correcto.
          </p>
        </div>

        {/* process line */}
        <div className="relative mt-16">
          <div className="hidden lg:block absolute top-[34px] left-0 right-0 h-px bg-mist"></div>
          <div className="hidden lg:block absolute top-[34px] left-0 h-px bg-copper reveal-line"></div>
          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-y-10 gap-x-6">
            {STEPS.map((s, i) => (
              <div key={s.n} className={"reveal reveal-d" + (i + 1) + " relative"}>
                <div className="flex items-center gap-4 mb-5">
                  <div className="relative z-10 flex items-center justify-center h-[68px] w-[68px] rounded-2xl border border-mist bg-white text-navy shadow-[0_10px_30px_-18px_rgba(11,29,45,.5)]">
                    <StepGlyph kind={s.kind} className="h-8 w-8" />
                  </div>
                  <span className="font-display text-[15px] font-bold text-copper">{s.n}</span>
                </div>
                <h3 className="font-display text-xl font-bold text-navy tracking-tightish">{s.title}</h3>
                <p className="mt-2.5 font-sans text-[15px] leading-[1.6] text-slate max-w-[15rem]">{s.body}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}

/* ---------------- MANIFIESTO ---------------- */
function Manifiesto() {
  return (
    <section className="relative bg-navy overflow-hidden py-28 lg:py-40">
      <div className="absolute inset-0 opacity-[0.06]" style={{ backgroundImage: "linear-gradient(to right, #fff 1px, transparent 1px), linear-gradient(to bottom, #fff 1px, transparent 1px)", backgroundSize: "80px 80px" }}></div>
      {/* watermark isotipo */}
      <img src="assets/adkun-mark.svg" alt="" aria-hidden="true"
           className="logo-light pointer-events-none select-none absolute -right-10 lg:right-16 top-1/2 -translate-y-1/2 h-[380px] lg:h-[460px] w-auto opacity-[0.06]" />
      <div className="relative mx-auto max-w-[1240px] px-6 lg:px-10">
        <div className="reveal flex items-center gap-3 mb-9">
          <span className="h-px w-10 bg-copper"></span>
          <span className="font-sans text-[12px] font-semibold tracking-[0.28em] text-white/55 uppercase">Manifiesto</span>
        </div>
        <h2 className="font-display font-bold tracking-tighter2 leading-[1.04] text-[clamp(2.4rem,6vw,5rem)]">
          <span className="reveal block text-white/40">La mejor tecnología no complica.</span>
          <span className="reveal reveal-d1 block text-white">Ordena.</span>
          <span className="reveal reveal-d2 block text-white">Conecta.</span>
          <span className="reveal reveal-d3 block text-copper">Impulsa.</span>
        </h2>
      </div>
    </section>
  );
}

/* ---------------- NOSOTROS ---------------- */
function Nosotros() {
  return (
    <section id="nosotros" data-anchor className="relative bg-white py-24 lg:py-32">
      <div className="mx-auto max-w-[1240px] px-6 lg:px-10 grid lg:grid-cols-2 gap-12 lg:gap-16 items-center">
        <div>
          <div className="reveal flex items-center gap-3 mb-6">
            <span className="h-px w-10 bg-copper"></span>
            <span className="font-sans text-[12px] font-semibold tracking-[0.28em] text-slate uppercase">Nosotros</span>
          </div>
          <h2 className="reveal reveal-d1 font-display font-bold text-navy tracking-tighter2 leading-[1.06] text-[clamp(1.9rem,4vw,3.1rem)]">
            Una empresa digital<br className="hidden md:block" /> construida desde el sur.
          </h2>
          <p className="reveal reveal-d2 mt-7 font-sans text-[1.08rem] leading-[1.7] text-slate max-w-[34rem]">
            ADKUN crea productos tecnológicos con vocación internacional y una mirada profundamente práctica: transformar operaciones complejas en sistemas claros, útiles y escalables.
          </p>
          <p className="reveal reveal-d3 mt-5 font-sans text-[1.08rem] leading-[1.7] text-slate max-w-[34rem]">
            Nuestra identidad nace desde el sur de Chile, pero nuestros productos están diseñados para crecer sin fronteras.
          </p>
          <div className="reveal reveal-d3 mt-8 flex items-center gap-3">
            <span className="h-1.5 w-1.5 rotate-45 bg-copper"></span>
            <span className="font-display text-[14.5px] font-medium text-navy/70">Orden que impulsa. <span className="text-copper">Tecnología con propósito.</span></span>
          </div>
        </div>

        <div className="reveal reveal-d2 relative">
          <div className="relative h-[300px] sm:h-[380px] lg:h-[440px] w-full overflow-hidden rounded-[20px] border border-navy/10 shadow-[0_40px_90px_-45px_rgba(11,29,45,.6)]">
            <FjordWide className="absolute inset-0 h-full w-full" />
            <div className="absolute inset-0" style={{ background: "linear-gradient(0deg, rgba(11,29,45,.65) 0%, rgba(11,29,45,0) 38%)" }}></div>
            <CornerTicks className="absolute left-5 top-5 h-6 w-6" color="#B45A2A" />
            <div className="absolute left-5 bottom-5 font-sans text-[11px] tracking-[0.22em] text-white/75 uppercase">Sur de Chile · Internacional</div>
          </div>
        </div>
      </div>
    </section>
  );
}

/* ---------------- CONTACTO ---------------- */
function Contacto() {
  const [form, setForm] = React.useState({ nombre: "", empresa: "", correo: "", mensaje: "" });
  const [touched, setTouched] = React.useState({});
  const [sent, setSent] = React.useState(false);

  const errs = {
    nombre: form.nombre.trim().length < 2 ? "Ingresa tu nombre" : "",
    correo: !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(form.correo) ? "Ingresa un correo válido" : "",
    mensaje: form.mensaje.trim().length < 4 ? "Cuéntanos brevemente" : "",
  };
  const valid = !errs.nombre && !errs.correo && !errs.mensaje;

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  const blur = (k) => () => setTouched((t) => ({ ...t, [k]: true }));

  const submit = (e) => {
    e.preventDefault();
    setTouched({ nombre: true, correo: true, mensaje: true, empresa: true });
    if (valid) setSent(true);
  };

  const field = "w-full rounded-xl border bg-white px-4 py-3 font-sans text-[15px] text-navy placeholder-slate/40 outline-none transition-colors";

  return (
    <section id="contacto" data-anchor className="relative bg-stone py-24 lg:py-32 overflow-hidden">
      <div className="absolute inset-0 grid-faint opacity-50"></div>
      <div className="relative mx-auto max-w-[1240px] px-6 lg:px-10 grid lg:grid-cols-[1fr_1fr] gap-12 lg:gap-16 items-start">
        <div className="lg:pt-4">
          <div className="reveal flex items-center gap-3 mb-6">
            <span className="h-px w-10 bg-copper"></span>
            <span className="font-sans text-[12px] font-semibold tracking-[0.28em] text-slate uppercase">Contacto</span>
          </div>
          <h2 className="reveal reveal-d1 font-display font-bold text-navy tracking-tighter2 leading-[1.06] text-[clamp(2rem,4.4vw,3.3rem)]">
            Construyamos algo que<br className="hidden md:block" /> ordene lo complejo.
          </h2>
          <p className="reveal reveal-d2 mt-6 font-sans text-[1.1rem] leading-[1.65] text-slate max-w-[34rem]">
            Conversemos sobre tu operación, tus procesos y las oportunidades que todavía no se han convertido en producto.
          </p>
          <div className="reveal reveal-d3 mt-8 flex flex-wrap gap-4">
            <a href="#contact-form" className="cta-primary btn-press group inline-flex items-center gap-2.5 rounded-full bg-navy px-7 py-3.5 text-[15px] font-semibold text-white hover:bg-copper shadow-[0_14px_30px_-14px_rgba(11,29,45,.85)]">
              Contactar a ADKUN <span className="transition-transform group-hover:translate-x-1">→</span>
            </a>
            <a href="#productos" className="btn-press inline-flex items-center gap-2 rounded-full border border-slate/30 bg-white px-7 py-3.5 text-[15px] font-semibold text-navy hover:border-navy">
              Conocer ALMA
            </a>
          </div>
        </div>

        {/* form card */}
        <div id="contact-form" className="reveal reveal-d2 relative rounded-3xl border border-mist bg-white p-7 lg:p-9 shadow-[0_40px_90px_-50px_rgba(11,29,45,.55)]">
          {sent ? (
            <div className="flex flex-col items-center text-center py-12">
              <div className="flex h-16 w-16 items-center justify-center rounded-full bg-copper/12 text-copper">
                <svg className="h-8 w-8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5"/></svg>
              </div>
              <h3 className="mt-6 font-display text-2xl font-bold text-navy">Mensaje enviado</h3>
              <p className="mt-3 font-sans text-[15px] text-slate max-w-[22rem]">Gracias, {form.nombre.split(" ")[0] || "te"}. Revisaremos tu mensaje y te responderemos pronto.</p>
              <button onClick={() => { setSent(false); setForm({ nombre: "", empresa: "", correo: "", mensaje: "" }); setTouched({}); }}
                      className="btn-press mt-7 font-sans text-[14px] font-semibold text-copper">Enviar otro mensaje</button>
            </div>
          ) : (
            <form onSubmit={submit} noValidate className="flex flex-col gap-4">
              <div className="grid sm:grid-cols-2 gap-4">
                <Field label="Nombre" required>
                  <input className={field + (touched.nombre && errs.nombre ? " border-copper" : " border-mist focus:border-navy")}
                         value={form.nombre} onChange={set("nombre")} onBlur={blur("nombre")} placeholder="Tu nombre" autoComplete="name" />
                  <FieldErr show={touched.nombre && errs.nombre}>{errs.nombre}</FieldErr>
                </Field>
                <Field label="Empresa">
                  <input className={field + " border-mist focus:border-navy"}
                         value={form.empresa} onChange={set("empresa")} placeholder="Tu organización" autoComplete="organization" />
                </Field>
              </div>
              <Field label="Correo" required>
                <input type="email" className={field + (touched.correo && errs.correo ? " border-copper" : " border-mist focus:border-navy")}
                       value={form.correo} onChange={set("correo")} onBlur={blur("correo")} placeholder="nombre@empresa.com" autoComplete="email" />
                <FieldErr show={touched.correo && errs.correo}>{errs.correo}</FieldErr>
              </Field>
              <Field label="Mensaje" required>
                <textarea rows="4" className={field + " resize-none " + (touched.mensaje && errs.mensaje ? "border-copper" : "border-mist focus:border-navy")}
                          value={form.mensaje} onChange={set("mensaje")} onBlur={blur("mensaje")} placeholder="Cuéntanos sobre tu operación y tus procesos." />
                <FieldErr show={touched.mensaje && errs.mensaje}>{errs.mensaje}</FieldErr>
              </Field>
              <button type="submit"
                      className="btn-press mt-2 inline-flex items-center justify-center gap-2.5 rounded-full bg-copper px-7 py-3.5 text-[15px] font-semibold text-white hover:bg-[#9d4a20] shadow-[0_14px_30px_-14px_rgba(180,90,42,.9)]">
                Enviar mensaje <span>→</span>
              </button>
            </form>
          )}
        </div>
      </div>
    </section>
  );
}

function Field({ label, required, children }) {
  return (
    <label className="block">
      <span className="mb-1.5 block font-sans text-[12.5px] font-semibold text-navy">
        {label}{required && <span className="text-copper"> *</span>}
      </span>
      {children}
    </label>
  );
}
function FieldErr({ show, children }) {
  if (!show) return null;
  return <span className="mt-1.5 block font-sans text-[12px] text-copper">{children}</span>;
}

/* ---------------- FOOTER ---------------- */
function Footer() {
  return (
    <footer className="relative bg-navy text-white/70 pt-20 pb-10 overflow-hidden">
      <div className="absolute inset-0 opacity-[0.05]" style={{ backgroundImage: "linear-gradient(to right, #fff 1px, transparent 1px), linear-gradient(to bottom, #fff 1px, transparent 1px)", backgroundSize: "72px 72px" }}></div>
      <div className="relative mx-auto max-w-[1240px] px-6 lg:px-10">
        <div className="grid gap-12 lg:grid-cols-[1.4fr_1fr_1fr] pb-14 border-b border-white/10">
          <div>
            <AdkunLogoImg className="h-7 w-auto" light />
            <p className="mt-5 font-display text-[15px] font-medium text-white max-w-[18rem]">Tecnología que ordena sistemas complejos.</p>
            <p className="mt-5 max-w-[20rem] font-sans text-[13.5px] leading-[1.6] text-white/45">
              ADKUN crea productos digitales que conectan operación, claridad y propósito.
            </p>
          </div>

          <div>
            <h4 className="font-sans text-[12px] font-semibold tracking-[0.22em] uppercase text-white/40 mb-5">Navegación</h4>
            <ul className="flex flex-col gap-3">
              {NAV_LINKS.map((l) => (
                <li key={l.href}><a href={l.href} className="navlink font-sans text-[14.5px] text-white/75 hover:text-white">{l.label}</a></li>
              ))}
              <li><a href="#productos" className="navlink font-sans text-[14.5px] font-semibold text-copper hover:text-[#d77a47]">ALMA — Gestión clínica</a></li>
            </ul>
          </div>

          <div>
            <h4 className="font-sans text-[12px] font-semibold tracking-[0.22em] uppercase text-white/40 mb-5">Contacto</h4>
            <ul className="flex flex-col gap-3 font-sans text-[14.5px]">
              <li>
                <span className="block text-white/40 text-[12px] mb-0.5">Correo</span>
                <a href="mailto:hola@adkun.com" className="navlink text-white/80 hover:text-white" data-editable-email>hola@adkun.com</a>
              </li>
              <li>
                <span className="block text-white/40 text-[12px] mb-0.5">LinkedIn</span>
                <a href="#" className="navlink text-white/80 hover:text-white" data-editable-linkedin>linkedin.com/company/adkun</a>
              </li>
            </ul>
          </div>
        </div>

        <div className="pt-7 flex flex-col sm:flex-row items-center justify-between gap-4">
          <p className="font-sans text-[13px] text-white/40">© 2026 ADKUN. Todos los derechos reservados.</p>
          <p className="font-sans text-[12px] tracking-[0.18em] uppercase text-white/35">Digital Products · Sur de Chile</p>
        </div>
      </div>
    </footer>
  );
}

Object.assign(window, { Enfoque, Manifiesto, Nosotros, Contacto, Footer });
