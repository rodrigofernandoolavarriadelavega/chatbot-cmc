/* ADKUN — ALMA featured product: detailed clinical dashboard in a browser frame. */

const ALMA_MODULES = [
  { id: "dashboard",   label: "Dashboard",     d: "M3 3h7v7H3zM14 3h7v4h-7zM14 11h7v10h-7zM3 14h7v7H3z" },
  { id: "pacientes",   label: "Pacientes",     d: "M16 11a4 4 0 10-8 0 M4 21c0-3.3 2.7-6 6-6s6 2.7 6 6 M12 7a4 4 0 100 0" },
  { id: "agenda",      label: "Agenda",        d: "M4 5h16v16H4zM4 9h16M8 3v4M16 3v4M9 14h2M14 14h1" },
  { id: "atenciones",  label: "Atenciones",    d: "M12 4v16M4 12h16" },
  { id: "ficha",       label: "Ficha clínica", d: "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h4" },
  { id: "caja",        label: "Caja",          d: "M3 7h18v12H3zM3 11h18M7 15h3" },
  { id: "facturacion", label: "Facturación",   d: "M7 3h10v18l-2-1.5L13 21l-2-1.5L9 21l-2-1.5zM10 8h4M10 12h4" },
  { id: "boxes",       label: "Boxes",         d: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z" },
  { id: "reportes",    label: "Reportes",      d: "M4 20V10M10 20V4M16 20v-7M22 20H2" },
  { id: "config",      label: "Configuración", d: "M12 8a4 4 0 100 8 4 4 0 000-8zM12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M19 5l-2 2M7 17l-2 2" },
];

const ALMA_KPIS = [
  { label: "Atenciones",      value: "32",   delta: "+12%" },
  { label: "Nuevos pacientes", value: "8",   delta: "+5%" },
  { label: "Horas utilizadas", value: "26 h", delta: "+8%" },
];

const ALMA_AGENDA = [
  { time: "09:00", type: "Control",    name: "María López",   status: "Completada", tone: "done" },
  { time: "10:30", type: "Evaluación", name: "Juan Pérez",    status: "En curso",   tone: "active" },
  { time: "11:30", type: "Control",    name: "Camila Rojas",  status: "Pendiente",  tone: "wait" },
  { time: "12:15", type: "Ingreso",    name: "Diego Salas",   status: "Pendiente",  tone: "wait" },
];

function MiniIcon({ d, className = "h-[17px] w-[17px]" }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

function AlmaDashboard() {
  return (
    <div className="overflow-hidden rounded-[14px] border border-mist bg-white shadow-[0_50px_110px_-50px_rgba(11,29,45,.65)]">
      {/* browser chrome */}
      <div className="flex items-center gap-3 border-b border-mist bg-stone px-4 py-3">
        <div className="flex gap-1.5">
          <span className="h-3 w-3 rounded-full bg-[#E0635A]"></span>
          <span className="h-3 w-3 rounded-full bg-[#E8B23E]"></span>
          <span className="h-3 w-3 rounded-full bg-[#5BB97E]"></span>
        </div>
        <div className="ml-2 flex-1 max-w-[280px] rounded-md bg-white border border-mist px-3 py-1 flex items-center gap-2">
          <svg className="h-3 w-3 text-slate/50" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 018 0v4"/></svg>
          <span className="font-sans text-[11.5px] text-slate/70">app.alma.adkun.com</span>
        </div>
      </div>

      {/* app body */}
      <div className="flex min-h-[460px]">
        {/* sidebar */}
        <aside className="w-[208px] shrink-0 bg-navy text-white/70 px-3.5 py-5 hidden sm:flex flex-col">
          <div className="flex items-center gap-2 px-2 pb-5 mb-2 border-b border-white/10">
            <span className="font-display text-[22px] font-bold text-white tracking-tight">alma</span>
            <span className="h-1.5 w-1.5 rotate-45 bg-copper translate-y-[3px]"></span>
          </div>
          <nav className="flex flex-col gap-0.5">
            {ALMA_MODULES.map((m, i) => (
              <div key={m.id}
                className={"flex items-center gap-3 rounded-lg px-3 py-[9px] text-[13px] font-medium transition-colors " +
                  (i === 0 ? "bg-white/[0.07] text-white" : "hover:bg-white/[0.04] hover:text-white/90")}>
                <span className={i === 0 ? "text-copper" : "text-white/45"}><MiniIcon d={m.d} /></span>
                <span>{m.label}</span>
                {i === 0 && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-copper"></span>}
              </div>
            ))}
          </nav>
          <div className="mt-auto pt-4 flex items-center gap-2.5 px-2 border-t border-white/10">
            <span className="h-7 w-7 rounded-full bg-copper/90 flex items-center justify-center text-[11px] font-bold text-white">DR</span>
            <div className="leading-tight">
              <div className="text-[12px] text-white font-medium">Dr. Daniel R.</div>
              <div className="text-[10.5px] text-white/45">Medicina general</div>
            </div>
          </div>
        </aside>

        {/* main */}
        <div className="flex-1 bg-[#FAFBFC] p-5 lg:p-6">
          <div className="flex items-center justify-between gap-3 mb-5">
            <div className="min-w-0">
              <div className="font-display text-[19px] font-bold text-navy whitespace-nowrap">Resumen del día</div>
              <div className="font-sans text-[12px] text-slate/60 whitespace-nowrap">Lunes 1 de junio · 2026</div>
            </div>
            <div className="flex items-center gap-2 rounded-full border border-mist bg-white px-3 py-1.5 shrink-0">
              <span className="h-6 w-6 rounded-full bg-navy flex items-center justify-center text-[10px] font-bold text-white">DR</span>
              <span className="font-sans text-[12.5px] font-medium text-navy">Dr. Daniel R.</span>
              <span className="text-slate/40 text-[11px]">▾</span>
            </div>
          </div>

          {/* KPI cards */}
          <div className="grid grid-cols-3 gap-3">
            {ALMA_KPIS.map((k, i) => (
              <div key={k.label} className="rounded-xl border border-mist bg-white p-3.5">
                <div className="font-sans text-[11px] text-slate/60 mb-1.5">{k.label}</div>
                <div className="flex items-end justify-between">
                  <span className="font-display text-[26px] font-bold text-navy leading-none whitespace-nowrap">{k.value}</span>
                  {i === 0 && (
                    <svg className="h-7 w-14" viewBox="0 0 56 28" fill="none" aria-hidden="true">
                      <path d="M0 22 L10 18 L20 21 L30 10 L40 14 L56 4" stroke="#B45A2A" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  )}
                </div>
                <div className="mt-1.5 font-sans text-[11px] font-semibold text-[#2E8B57]">{k.delta} <span className="text-slate/40 font-normal">vs ayer</span></div>
              </div>
            ))}
          </div>

          {/* agenda */}
          <div className="mt-5 rounded-xl border border-mist bg-white">
            <div className="flex items-center justify-between gap-2 px-4 py-3 border-b border-mist">
              <span className="font-display text-[14px] font-bold text-navy whitespace-nowrap">Agenda de hoy</span>
              <a className="font-sans text-[11.5px] font-medium text-copper cursor-default whitespace-nowrap">Ver agenda completa →</a>
            </div>
            <div>
              {ALMA_AGENDA.map((a) => (
                <div key={a.time} className="flex items-center gap-4 px-4 py-[11px] border-b border-stone last:border-0 hover:bg-stone/60 transition-colors">
                  <span className={"w-1 self-stretch rounded-full " + (a.tone === "active" ? "bg-copper" : a.tone === "done" ? "bg-[#5BB97E]" : "bg-mist")}></span>
                  <span className="font-sans text-[12.5px] font-semibold text-navy w-[44px]">{a.time}</span>
                  <span className="font-sans text-[12.5px] text-slate w-[78px]">{a.type}</span>
                  <span className="font-sans text-[12.5px] text-navy flex-1">{a.name}</span>
                  <StatusBadge tone={a.tone}>{a.status}</StatusBadge>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function StatusBadge({ tone, children }) {
  const map = {
    done:   "bg-[#E7F3EC] text-[#2E8B57]",
    active: "bg-copper/12 text-copper",
    wait:   "bg-stone text-slate/70",
  };
  return (
    <span className={"shrink-0 rounded-full px-2.5 py-1 font-sans text-[10.5px] font-semibold " + (map[tone] || map.wait)}>
      {children}
    </span>
  );
}

function Alma() {
  const chips = ALMA_MODULES.map((m) => m.label);
  return (
    <section id="productos" data-anchor className="relative bg-stone py-24 lg:py-32 overflow-hidden">
      <div className="absolute inset-0 grid-faint opacity-50"></div>
      <div className="relative mx-auto max-w-[1240px] px-6 lg:px-10 grid lg:grid-cols-[0.82fr_1.18fr] gap-12 lg:gap-14 items-center">
        {/* text column */}
        <div>
          <span className="reveal inline-flex items-center gap-2 rounded-full border border-slate/20 bg-white px-3.5 py-1.5 font-sans text-[11.5px] font-semibold tracking-[0.12em] uppercase text-slate">
            <span className="h-1.5 w-1.5 rotate-45 bg-copper"></span>
            Producto ADKUN para gestión clínica
          </span>
          <div className="reveal reveal-d1 mt-6 flex items-end gap-3">
            <h2 className="font-display font-bold text-navy tracking-tighter2 leading-none text-[clamp(3rem,7vw,5rem)]">ALMA</h2>
            <span className="mb-2 h-2 w-2 rotate-45 bg-copper"></span>
          </div>
          <p className="reveal reveal-d1 mt-2 font-display text-[clamp(1.25rem,2.4vw,1.7rem)] font-medium text-slate leading-[1.2]">
            El espíritu operativo de tu centro médico.
          </p>
          <p className="reveal reveal-d2 mt-6 font-sans text-[1.05rem] leading-[1.65] text-slate max-w-[34rem]">
            ALMA es una plataforma de gestión clínica desarrollada por ADKUN. Integra agenda, pacientes, ficha clínica, caja, boxes y atención digital en un solo lugar.
          </p>

          <div className="reveal reveal-d3 mt-7 flex flex-wrap gap-2">
            {chips.map((c) => (
              <span key={c} className="rounded-md border border-mist bg-white px-2.5 py-1 font-sans text-[12px] font-medium text-slate/80">{c}</span>
            ))}
          </div>

          <div className="reveal reveal-d3 mt-8 flex flex-wrap gap-4">
            <a href="#contacto" className="cta-primary btn-press group inline-flex items-center gap-2.5 rounded-full bg-navy px-7 py-3.5 text-[15px] font-semibold text-white hover:bg-copper shadow-[0_14px_30px_-14px_rgba(11,29,45,.85)]">
              Conocer ALMA <span className="transition-transform group-hover:translate-x-1">→</span>
            </a>
            <a href="#contacto" className="btn-press inline-flex items-center gap-2 rounded-full border border-slate/30 bg-white px-7 py-3.5 text-[15px] font-semibold text-navy hover:border-navy">
              Solicitar una demostración
            </a>
          </div>
        </div>

        {/* dashboard */}
        <div className="reveal reveal-d2 relative">
          <AlmaDashboard />
        </div>
      </div>
    </section>
  );
}

Object.assign(window, { Alma });
