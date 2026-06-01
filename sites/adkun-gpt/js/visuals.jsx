/* ADKUN — visual primitives: abstract southern-Chile landscapes, line icons, marks.
   Exported to window at the bottom for cross-file use. */

/* ---------- Inline brand mark (recolorable) ---------- */
function AdkunMarkImg({ className = "h-9 w-auto", light = false }) {
  return (
    <img
      src="assets/adkun-mark.svg"
      alt="ADKUN"
      className={className + (light ? " logo-light" : "")}
      draggable="false"
    />
  );
}
function AdkunLogoImg({ className = "h-7 w-auto", light = false }) {
  return (
    <img
      src="assets/adkun-logo.svg"
      alt="ADKUN — Digital Products"
      className={className + (light ? " logo-light" : "")}
      draggable="false"
    />
  );
}

/* ---------- Fine survey-grid + tick marks (decorative) ---------- */
function CornerTicks({ className = "", color = "#B45A2A" }) {
  return (
    <svg className={className} viewBox="0 0 40 40" fill="none" aria-hidden="true">
      <path d="M0 12 V0 H12" stroke={color} strokeWidth="1.2" />
      <circle cx="20" cy="20" r="2" fill={color} />
    </svg>
  );
}

/* ---------- Generated atmospheric fjord imagery (procedural canvas art) ---------- */
function FjordScene({ className = "" }) {
  return (
    <img src="assets/hero-fjord.png" alt="Fiordo del sur de Chile entre la niebla, montañas y agua en calma"
         className={className + " object-cover"} draggable="false" />
  );
}

function FjordWide({ className = "" }) {
  return (
    <img src="assets/about-fjord.png" alt="Fiordo nocturno del sur con cielo estrellado y luna cobre"
         className={className + " object-cover"} draggable="false" />
  );
}

/* ---------- Line icons (1.5 stroke, currentColor) ---------- */
function IconConnected({ className = "" }) {
  return (
    <svg className={className} viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="24" cy="10" r="4" />
      <circle cx="10" cy="36" r="4" />
      <circle cx="38" cy="36" r="4" />
      <path d="M24 14 V24 M24 24 L12 33 M24 24 L36 33" />
      <circle cx="24" cy="24" r="2.4" fill="currentColor" stroke="none" />
    </svg>
  );
}
function IconPurpose({ className = "" }) {
  return (
    <svg className={className} viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="24" cy="24" r="16" />
      <path d="M30 18 L22 22 L18 30 L26 26 Z" />
      <circle cx="24" cy="24" r="1.6" fill="currentColor" stroke="none" />
    </svg>
  );
}
function IconScale({ className = "" }) {
  return (
    <svg className={className} viewBox="0 0 48 48" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M8 40 H40" />
      <rect x="11" y="28" width="7" height="12" />
      <rect x="22" y="20" width="7" height="20" />
      <rect x="33" y="12" width="7" height="28" />
      <path d="M9 22 L19 14 L28 19 L41 7" />
      <path d="M35 7 H41 V13" />
    </svg>
  );
}

/* ---------- Process step glyphs ---------- */
function StepGlyph({ kind, className = "" }) {
  const common = { className, viewBox: "0 0 48 48", fill: "none", stroke: "currentColor", strokeWidth: "1.5", strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true };
  if (kind === "understand")
    return (<svg {...common}><circle cx="21" cy="21" r="12" /><path d="M30 30 L40 40" /><path d="M21 15 V21 L25 24" /></svg>);
  if (kind === "order")
    return (<svg {...common}><path d="M10 14 H38 M10 24 H30 M10 34 H22" /><path d="M34 30 L37 33 L42 27" /></svg>);
  if (kind === "design")
    return (<svg {...common}><rect x="8" y="10" width="32" height="24" rx="2" /><path d="M8 18 H40 M14 14 H14.01 M18 14 H18.01" /><path d="M18 40 H30 M24 34 V40" /></svg>);
  return (<svg {...common}><path d="M24 38 V14" /><path d="M16 22 L24 14 L32 22" /><path d="M10 38 H38" /><circle cx="24" cy="9" r="2.2" /></svg>);
}

Object.assign(window, {
  AdkunMarkImg, AdkunLogoImg, CornerTicks,
  FjordScene, FjordWide,
  IconConnected, IconPurpose, IconScale, StepGlyph,
});
