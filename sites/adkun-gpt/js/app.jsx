/* ADKUN — app composition + scroll-reveal + tweaks. */

function useScrollReveal(motion) {
  React.useEffect(() => {
    const reveal = (el) => {
      el.classList.add("in");
      if (el.classList.contains("reveal-line")) el.style.width = "78%";
      else el.style.opacity = "1";
    };
    const els = document.querySelectorAll(".reveal, .reveal-line");
    // reduced motion o sin IntersectionObserver → mostrar todo de inmediato
    if (motion === "reduced" || typeof IntersectionObserver === "undefined") {
      els.forEach(reveal);
      return;
    }
    // IntersectionObserver es fiable en móvil (incl. scroll con inercia en iOS),
    // donde la matemática de posición de scroll puede dejar secciones invisibles.
    const io = new IntersectionObserver((entries) => {
      entries.forEach((e) => {
        if (e.isIntersecting) { reveal(e.target); io.unobserve(e.target); }
      });
    }, { rootMargin: "0px 0px -8% 0px", threshold: 0.01 });
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [motion]);
}

const TWEAK_DEFAULTS = /*EDITMODE-BEGIN*/{
  "heroVisual": "fjord",
  "accent": "estandar",
  "motion": "on"
}/*EDITMODE-END*/;

function App() {
  const [t, setTweak] = useTweaks(TWEAK_DEFAULTS);
  useScrollReveal(t.motion === "on" ? "on" : "reduced");

  const wrapClass =
    (t.motion === "on" ? "" : "motion-reduced ") +
    (t.accent === "intenso" ? "accent-intense" : "");

  return (
    <div className={wrapClass}>
      <Nav />
      <main>
        <Hero visual={t.heroVisual} />
        <ValueProp />
        <Alma />
        <Enfoque />
        <Manifiesto />
        <Nosotros />
        <Contacto />
      </main>
      <Footer />

      <TweaksPanel title="Tweaks">
        <TweakSection label="Hero" />
        <TweakRadio
          label="Visual"
          value={t.heroVisual}
          options={[{ value: "fjord", label: "Fiordo" }, { value: "minimal", label: "Isotipo" }]}
          onChange={(v) => setTweak("heroVisual", v)}
        />
        <TweakSection label="Acento cobre" />
        <TweakRadio
          label="Intensidad"
          value={t.accent}
          options={[{ value: "estandar", label: "Estándar" }, { value: "intenso", label: "Intenso" }]}
          onChange={(v) => setTweak("accent", v)}
        />
        <TweakSection label="Movimiento" />
        <TweakRadio
          label="Animaciones"
          value={t.motion}
          options={[{ value: "on", label: "Activadas" }, { value: "reduced", label: "Reducidas" }]}
          onChange={(v) => setTweak("motion", v)}
        />
      </TweaksPanel>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
