/* ADKUN — app composition + scroll-reveal + tweaks. */

function useScrollReveal(motion) {
  React.useEffect(() => {
    const reveal = (el) => {
      el.classList.add("in");
      if (el.classList.contains("reveal-line")) el.style.width = "78%";
      else el.style.opacity = "1";
    };
    if (motion === "reduced") {
      document.querySelectorAll(".reveal, .reveal-line").forEach(reveal);
      return;
    }
    const check = () => {
      const vh = window.innerHeight || document.documentElement.clientHeight;
      document.querySelectorAll(".reveal:not(.in), .reveal-line:not(.in)").forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.top < vh * 0.9 && r.bottom > -40) reveal(el);
      });
    };
    check();
    window.addEventListener("scroll", check, { passive: true });
    window.addEventListener("resize", check);
    // catch late layout shifts (web-font load, image decode)
    const id = setInterval(check, 350);
    const stop = setTimeout(() => clearInterval(id), 4000);
    return () => {
      window.removeEventListener("scroll", check);
      window.removeEventListener("resize", check);
      clearInterval(id);
      clearTimeout(stop);
    };
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
