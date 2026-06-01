/* ADKUN — sections part 1: Nav, Hero, Value proposition, ALMA product. */

const NAV_LINKS = [
  { href: "#productos", label: "Productos" },
  { href: "#enfoque",   label: "Enfoque" },
  { href: "#nosotros",  label: "Nosotros" },
  { href: "#contacto",  label: "Contacto" },
];

/* ---------------- NAV ---------------- */
function Nav() {
  const [scrolled, setScrolled] = React.useState(false);
  const [open, setOpen] = React.useState(false);
  React.useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 16);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);
  React.useEffect(() => {
    document.body.style.overflow = open ? "hidden" : "";
  }, [open]);

  return (
    <header className={"fixed top-0 inset-x-0 z-50 transition-all duration-500 " +
      (scrolled ? "bg-white/80 backdrop-blur-md border-b border-mist/70 shadow-[0_1px_24px_-12px_rgba(11,29,45,.4)]" : "bg-transparent")}>
      <nav className="mx-auto max-w-[1240px] px-6 lg:px-10 flex items-center justify-between h-[72px]">
        <a href="#top" className="flex items-center gap-3 shrink-0" aria-label="ADKUN inicio">
          <AdkunLogoImg className="h-[26px] w-auto" />
        </a>

        <div className="hidden md:flex items-center gap-9">
          {NAV_LINKS.map((l) => (
            <a key={l.href} href={l.href}
               className="navlink font-sans text-[14.5px] font-medium text-slate hover:text-navy transition-colors">
              {l.label}
            </a>
          ))}
        </div>

        <div className="hidden md:block">
          <a href="#contacto"
             className="btn-press inline-flex items-center gap-2 rounded-full bg-copper px-5 py-2.5 text-[14px] font-semibold text-white hover:bg-[#9d4a20] shadow-[0_8px_20px_-10px_rgba(180,90,42,.8)]">
            Conversemos
            <span className="text-base leading-none">→</span>
          </a>
        </div>

        {/* mobile toggle */}
        <button onClick={() => setOpen(true)} className="md:hidden inline-flex flex-col gap-[5px] p-2 -mr-2" aria-label="Abrir menú">
          <span className="block h-[1.5px] w-6 bg-navy"></span>
          <span className="block h-[1.5px] w-6 bg-navy"></span>
          <span className="block h-[1.5px] w-4 bg-navy"></span>
        </button>
      </nav>

      {/* mobile drawer */}
      <div className={"md:hidden fixed inset-0 z-50 transition-opacity duration-300 " + (open ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none")}>
        <div className="absolute inset-0 bg-navy/40 backdrop-blur-sm" onClick={() => setOpen(false)}></div>
        <div className={"absolute top-0 right-0 h-full w-[82%] max-w-[360px] bg-white shadow-2xl p-7 flex flex-col transition-transform duration-400 " + (open ? "translate-x-0" : "translate-x-full")}>
          <div className="flex items-center justify-between mb-10">
            <AdkunLogoImg className="h-6 w-auto" />
            <button onClick={() => setOpen(false)} className="p-2 -mr-2 text-2xl text-slate" aria-label="Cerrar menú">✕</button>
          </div>
          <div className="flex flex-col gap-1">
            {NAV_LINKS.map((l) => (
              <a key={l.href} href={l.href} onClick={() => setOpen(false)}
                 className="font-display text-2xl font-medium text-navy py-3 border-b border-stone">
                {l.label}
              </a>
            ))}
          </div>
          <a href="#contacto" onClick={() => setOpen(false)}
             className="btn-press mt-8 inline-flex items-center justify-center gap-2 rounded-full bg-copper px-5 py-3.5 text-[15px] font-semibold text-white">
            Conversemos →
          </a>
        </div>
      </div>
    </header>
  );
}

/* ---------------- HERO ---------------- */
function Hero({ visual = "fjord" }) {
  const blended = visual !== "minimal";
  return (
    <section id="top" data-anchor className="relative overflow-hidden bg-stone">
      <div className="absolute inset-0 grid-faint opacity-70"></div>

      {/* fjord bleed (desktop) — sangra al borde derecho y se funde con el fondo */}
      {blended && (
        <div aria-hidden="true" className="hidden lg:block absolute inset-0">
          <FjordScene className="absolute inset-0 h-full w-full" />
          <div className="absolute inset-0" style={{ background: "linear-gradient(90deg, #F2F3F5 0%, rgba(242,243,245,0.92) 30%, rgba(242,243,245,0.45) 50%, rgba(242,243,245,0) 66%)" }}></div>
          <div className="absolute inset-0" style={{ background: "linear-gradient(180deg, #F2F3F5 0%, rgba(242,243,245,0) 20%)" }}></div>
          <div className="absolute inset-0" style={{ background: "linear-gradient(0deg, #F2F3F5 0%, rgba(242,243,245,0) 18%)" }}></div>
          <div className="absolute inset-0" style={{ background: "linear-gradient(270deg, #F2F3F5 0%, rgba(242,243,245,0) 9%)" }}></div>
        </div>
      )}

      <div className="relative z-10 mx-auto max-w-[1240px] px-6 lg:px-10 grid lg:grid-cols-[1.05fr_.95fr] gap-10 lg:gap-16 items-center pt-32 pb-20 lg:pt-40 lg:pb-28">
        {/* text */}
        <div>
          <div className="reveal flex items-center gap-3 mb-7">
            <span className="h-px w-10 bg-copper"></span>
            <span className="font-sans text-[12px] font-semibold tracking-[0.28em] text-slate uppercase">ADKUN · Digital Products</span>
          </div>
          <h1 className="reveal reveal-d1 font-display font-bold text-navy tracking-tighter2 leading-[1.02] text-[clamp(2.6rem,6vw,4.6rem)]">
            Tecnología que ordena<br className="hidden sm:block" /> sistemas <span className="relative inline-block">complejos<span className="text-copper">.</span></span>
          </h1>
          <p className="reveal reveal-d2 mt-7 max-w-[34rem] font-sans text-[clamp(1.05rem,1.5vw,1.2rem)] leading-[1.6] text-slate">
            Creamos productos digitales que conectan procesos, datos y personas para que las organizaciones trabajen con mayor claridad.
          </p>
          <div className="reveal reveal-d3 mt-9 flex flex-wrap items-center gap-4">
            <a href="#productos"
               className="cta-primary btn-press group inline-flex items-center gap-2.5 rounded-full bg-navy px-7 py-3.5 text-[15px] font-semibold text-white hover:bg-copper shadow-[0_14px_30px_-14px_rgba(11,29,45,.85)]">
              Conoce nuestros productos
              <span className="transition-transform group-hover:translate-x-1">→</span>
            </a>
            <a href="#contacto"
               className="btn-press inline-flex items-center gap-2 rounded-full border border-slate/30 bg-white/60 px-7 py-3.5 text-[15px] font-semibold text-navy hover:border-navy hover:bg-white">
              Hablemos
            </a>
          </div>
          <div className="reveal reveal-d4 mt-10 flex items-center gap-3">
            <span className="h-1.5 w-1.5 rotate-45 bg-copper"></span>
            <p className="font-display text-[14.5px] font-medium tracking-wide text-navy/70">
              Orden que impulsa. <span className="text-copper">Tecnología con propósito.</span>
            </p>
          </div>
        </div>

        {/* visual */}
        {blended ? (
          // mobile/tablet: el fiordo se muestra apilado y difuminado hacia el fondo
          <div className="reveal reveal-d2 relative lg:hidden -mx-6 -mt-2">
            <div className="relative w-full aspect-[1672/941] overflow-hidden">
              <FjordScene className="absolute inset-0 h-full w-full" />
              {/* se funde con el fondo arriba y abajo, sangra a los bordes (integrada, no tarjeta) */}
              <div className="absolute inset-0" style={{ background: "linear-gradient(180deg, #F2F3F5 0%, rgba(242,243,245,0) 26%)" }}></div>
              <div className="absolute inset-0" style={{ background: "linear-gradient(0deg, #F2F3F5 0%, rgba(242,243,245,0) 22%)" }}></div>
            </div>
          </div>
        ) : (
          <div className="reveal reveal-d2 relative">
            <div className="relative h-[360px] sm:h-[440px] lg:h-[560px] w-full overflow-hidden rounded-[20px] border border-white shadow-[0_40px_80px_-40px_rgba(11,29,45,.55)] bg-navy">
              <div className="absolute inset-0 opacity-[0.10]" style={{ backgroundImage: "linear-gradient(to right, #fff 1px, transparent 1px), linear-gradient(to bottom, #fff 1px, transparent 1px)", backgroundSize: "56px 56px" }}></div>
              <img src="assets/adkun-mark.svg" alt="" aria-hidden="true" className="logo-light absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 h-[58%] w-auto opacity-90" />
              <span className="absolute left-1/2 top-1/2 h-px w-[70%] -translate-x-1/2 -translate-y-1/2 bg-copper/40"></span>
              <span className="absolute left-1/2 top-1/2 h-[70%] w-px -translate-x-1/2 -translate-y-1/2 bg-copper/40"></span>
              <CornerTicks className="absolute left-5 top-5 h-6 w-6" />
            </div>
          </div>
        )}
      </div>
    </section>
  );
}

/* ---------------- VALUE PROPOSITION ---------------- */
const VALUES = [
  { Icon: IconConnected, title: "Operación conectada", body: "Centralizamos procesos, información y equipos en una sola experiencia digital." },
  { Icon: IconPurpose,   title: "Diseño con propósito", body: "Creamos productos simples de usar, incluso cuando resuelven problemas complejos." },
  { Icon: IconScale,     title: "Escalabilidad real",   body: "Construimos plataformas preparadas para crecer junto a cada organización." },
];

function ValueProp() {
  return (
    <section className="relative bg-white py-24 lg:py-32">
      <div className="mx-auto max-w-[1240px] px-6 lg:px-10">
        <div className="max-w-[46rem]">
          <div className="reveal flex items-center gap-3 mb-6">
            <span className="h-px w-10 bg-copper"></span>
            <span className="font-sans text-[12px] font-semibold tracking-[0.28em] text-slate uppercase">Propuesta de valor</span>
          </div>
          <h2 className="reveal reveal-d1 font-display font-bold text-navy tracking-tighter2 leading-[1.06] text-[clamp(1.9rem,4vw,3.1rem)]">
            Convertimos complejidad en<br className="hidden md:block" /> claridad operativa.
          </h2>
          <p className="reveal reveal-d2 mt-6 font-sans text-[1.1rem] leading-[1.65] text-slate max-w-[40rem]">
            Diseñamos plataformas que permiten ordenar procesos, centralizar información y simplificar decisiones. Tecnología pensada para trabajar mejor, no para agregar más fricción.
          </p>
        </div>

        <div className="mt-14 grid md:grid-cols-3 gap-5 lg:gap-6">
          {VALUES.map((v, i) => (
            <article key={v.title}
              className={"reveal reveal-d" + (i + 1) + " card-lift group relative rounded-2xl border border-mist bg-white p-8 hover:-translate-y-1.5 hover:shadow-[0_30px_60px_-30px_rgba(11,29,45,.4)] hover:border-slate/30"}>
              <div className="flex items-center justify-center h-14 w-14 rounded-xl bg-stone text-navy group-hover:bg-navy group-hover:text-copper transition-colors duration-400">
                <v.Icon className="h-7 w-7" />
              </div>
              <h3 className="mt-7 font-display text-xl font-bold text-navy tracking-tightish">{v.title}</h3>
              <p className="mt-3 font-sans text-[15px] leading-[1.6] text-slate">{v.body}</p>
              <div className="mt-7 h-px w-full bg-mist overflow-hidden">
                <div className="h-full w-0 bg-copper group-hover:w-full transition-all duration-700 ease-out"></div>
              </div>
              <span className="mt-3 inline-block font-display text-[13px] font-semibold text-slate/50">0{i + 1}</span>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}

Object.assign(window, { Nav, Hero, ValueProp });
