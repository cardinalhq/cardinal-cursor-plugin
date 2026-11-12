// Exemplar 4 of 4: a milestone illustration in the anatomy style, on the
// `iso` library (libraries: ["iso"], global Iso). Read it for the method —
// one physical machine, its parts sized by bound values, extruded with the
// kit, a plate card under it, numbers marked, one reveal — not the look.
//
// Bindings this surface expects (pass them to storyboard__set_milestone_visual):
//   p99        {receiptId, selector: "/data_points/0/metrics/value"}  a scalar
//   objective  {receiptId, selector: "/rules/0/threshold"}            a scalar
// config: {fig, title, unit}
//
// A milestone illustration in anatomy's style, drawn with the `iso` library
// (the vendored anatomy kit, global Iso): a panel gauge on a bench plate.
// The red set hand is the objective, the lit needle is the measured p99, and
// a probe block on the plate is wired into the gauge case. Every number it
// shows is a bound value (p99, objective) or a scale derived from them.
//   step 0: the objective is set and the needle rests at zero
//   step 1: the needle swings to the measured p99
// docs/specs/storyboard-milestones.md, "Illustrated milestones".

const { p99, objective } = await cv.data(["p99", "objective"]);
for (const [key, ev] of [["p99", p99], ["objective", objective]]) {
  if (typeof ev.value !== "number" || !Number.isFinite(ev.value) || ev.value < 0) {
    throw new TypeError(`gauge: binding "${key}" must be a non-negative number, got ${JSON.stringify(ev.value)}`);
  }
}
const cfg = cv.config || {};
const unit = cfg.unit || "ms";
const fmt = (v) => Math.round(v).toLocaleString("en-US");

// ---- the model: value -> needle angle ------------------------------------
// A 270° scale with 8 major steps, its full scale the first nice step that
// clears both values with headroom.
const NICE = [10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 20000, 25000, 50000];
const want = (Math.max(p99.value, objective.value) * 1.2) / 8;
const STEP = NICE.find((s) => s >= want) || NICE[NICE.length - 1];
const FULL = STEP * 8;
const SPAN = 1.5 * Math.PI;
// FROM is set once the camera is fitted: the scale's open quarter faces the
// viewer, as on a real gauge.
let FROM = 0;
const angleOf = (v) => FROM + (Math.min(Math.max(v, 0), FULL) / FULL) * SPAN;

// ---- world ----------------------------------------------------------------
const W = 640;
const H = 400;
const PLATE = { x: -120, y: -78, w: 240, d: 156, r: 8 };
const PLATE_H = 10;
const FOOT_H = 4;
const C = [14, -4];
const R = 54;
const CASE_H = 14;
const BEZEL_H = 4;
const TOP = CASE_H + BEZEL_H;
const FACE = TOP - 1.2;
const PROBE = { x: -104, y: -62, w: 34, d: 28, r: 3 };
const PROBE_H = 16;
const KNOB = [-84, 50];
const LABEL = { x: 58, y: 48, w: 48, d: 20, r: 1.5 };

const P = Iso.fitProjection(
  [...Iso.boxCorners(PLATE, -PLATE_H - FOOT_H, 0), [C[0] - R, C[1], TOP], [C[0] + R, C[1], TOP], [C[0], C[1] - R, TOP + 4]],
  W,
  H,
  { pad: 26, azimuth: 42 },
);
{
  // Angles run from -y (sin a, -cos a); the open quarter is centred on the
  // floor direction toward the viewer, and the scale climbs clockwise.
  const [vx, vy] = Iso.towardViewer(P);
  FROM = Math.atan2(vx, -vy) + SPAN / 6;
}
const at = (p) => Iso.iso(p, P);
const S = (paths, style) => Iso.solidSvg(paths, style);
const L = (d, style) => Iso.lineSvg(d, style);
const D = (pts, style) => Iso.dotsSvg(pts, style);
const byDepth = (pts) => [...pts].sort((a, b) => Iso.depthOf([a[0], a[1], 0], P) - Iso.depthOf([b[0], b[1], 0], P));
const polar = (r, a) => [C[0] + Math.sin(a) * r, C[1] - Math.cos(a) * r];
const arcOnFace = (r, a0, a1, z, n = 48) => {
  const pts = [];
  for (let i = 0; i <= n; i++) pts.push(polar(r, a0 + ((a1 - a0) * i) / n));
  return Iso.onTop(pts, z, P);
};

// ---- parts ----------------------------------------------------------------
function plate() {
  const out = [`<path class="iso-halo" d="${Iso.haloOf(PLATE, -PLATE_H - FOOT_H, PLATE_H, P)}"/>`];
  for (const [x, y] of byDepth(Iso.corners(PLATE, 22))) {
    out.push(S(Iso.cylinder(x, y, 7, -PLATE_H - FOOT_H, 1.6, P, 28), { tone: "lo" }));
    out.push(S(Iso.cylinder(x, y, 5.4, -PLATE_H - FOOT_H + 1.6, FOOT_H - 1.6, P, 28), { tone: "mid" }));
  }
  out.push(S(Iso.slabOf(PLATE, -PLATE_H, PLATE_H, P, 8, 1.6), { tone: "mid" }));
  out.push(L(Iso.planOutline(Iso.insetPlan(PLATE, 5), 0, P), { tone: "lo" }));
  const screws = Iso.corners(PLATE, 10);
  out.push(L(screws.map(([x, y]) => Iso.ring(x, y, 2.2, 0, P, 16)).join(""), { tone: "lo" }));
  out.push(D(screws.map(([x, y]) => at([x, y, 0])), { size: 0.5 }));
  // A ruler strip along the back edge.
  const strip = { x: -60, y: PLATE.y + 6, w: 160, d: 7, r: 1 };
  out.push(S(Iso.slabOf(strip, 0, 0.6, P, 3), { tone: "lo", crease: "none" }));
  const ticks = Iso.topTicks(strip.x + 4, strip.x + strip.w - 4, 4, 5, strip.y + strip.d - 0.8, 0.6, [-2, -3.6], P);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  out.push(D([at([strip.x + 2, strip.y + 3.5, 0.6]), at([strip.x + strip.w - 2, strip.y + 3.5, 0.6])], { size: 0.45 }));
  return out.join("");
}

function probe() {
  const out = [];
  out.push(S(Iso.slabOf(PROBE, 0, 3, P, 4, 0.6), { tone: "mid" }));
  const body = Iso.insetPlan(PROBE, 2.5);
  out.push(S(Iso.slabOf(body, 3, PROBE_H - 3, P, 4, 0.8), { tone: "mid" }));
  out.push(L(Iso.planOutline(Iso.insetPlan(body, 3), PROBE_H, P), { tone: "faint" }));
  // Vent slots on the top, closed at both ends.
  for (let i = 0; i < 4; i++) {
    const y = body.y + 6 + i * 3.4;
    out.push(L(Iso.lineOnTop([body.x + 6, y], [body.x + body.w - 10, y], PROBE_H, P), { tone: "lo" }));
  }
  out.push(D([at([body.x + body.w - 5, body.y + 5, PROBE_H])], { size: 0.8, tone: "hi" }));
  // Connector shell on the gauge-facing side.
  const shellX = PROBE.x + PROBE.w;
  out.push(S(Iso.cylinder(shellX + 2, PROBE.y + PROBE.d / 2, 3, 6, 5, P, 20), { tone: "mid" }));
  for (const [x, y] of Iso.corners(PROBE, 2.5)) out.push(D([at([x, y, 3])], { size: 0.4, tone: "lo" }));
  return out.join("");
}

function cable() {
  // From the probe's connector shell, across the plate, into the gauge case.
  const from = at([PROBE.x + PROBE.w + 4, PROBE.y + PROBE.d / 2, 8.5]);
  const entry = [C[0] - Math.cos(0.9) * R, C[1] - Math.sin(0.9) * R];
  const to = at([entry[0], entry[1], 6]);
  const via = [at([-50, -46, 0.8]), at([entry[0] - 8, entry[1] - 4, 0.8])];
  return Iso.wireSvg(from, to, { via, tone: "mid", size: 1.1 });
}

function gauge() {
  const out = [];
  const [x, y] = C;
  out.push(`<path class="iso-halo" d="${Iso.haloOf({ x: x - R, y: y - R, w: R * 2, d: R * 2, r: R }, 0, 0.1, P)}" style="filter:blur(5px)"/>`);
  out.push(S(Iso.cylinder(x, y, R - 3, 0, 2, P, 72), { tone: "lo" }));
  out.push(S(Iso.cylinder(x, y, R - 1, 2, CASE_H - 2, P, 72, 0.6), { tone: "mid" }));
  out.push(L(Iso.sideArc(x, y, R - 1, 7, P, 48), { tone: "faint" }));
  out.push(S(Iso.cylinder(x, y, R, CASE_H, BEZEL_H, P, 96, 0.8), { tone: "hi", crease: "lo" }));
  out.push(L(Iso.knurl(x, y, R, CASE_H + 0.6, TOP - 0.6, 120, P, 0.3), { tone: "lo" }));
  // The dial face, sunk into the bezel.
  out.push(L(Iso.ring(x, y, R - 3, TOP, P, 96), { tone: "lo" }));
  out.push(L(Iso.ring(x, y, R - 4.2, FACE, P, 96), { tone: "faint" }));
  // The objective's red zone, from the set point to full scale.
  out.push(`<g data-cv-anchor="zone">${L(arcOnFace(R - 7.5, angleOf(objective.value), FROM + SPAN, FACE), { tone: "red", className: "zone" })}</g>`);
  const ticks = Iso.radialTicks(x, y, R - 5, 40, 5, FACE, [1.6, 3.6], P, FROM, SPAN);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  for (let i = 0; i <= 8; i++) {
    const [lx, ly] = polar(R - 15, FROM + (i / 8) * SPAN);
    out.push(Iso.faceTextSvg(Iso.topMatrix([lx, ly + 1.2], FACE, P, "x"), fmt(STEP * i), { size: 3.4, tone: "mid", anchor: "middle" }));
  }
  out.push(Iso.faceTextSvg(Iso.topMatrix([x, y + 18], FACE, P, "x"), `P99 · ${unit.toUpperCase()}`, { size: 3, tone: "lo", anchor: "middle" }));
  // Bezel screws.
  out.push(D([0.3, 2.4, 4.5].map((a) => at([...polar(R - 1.8, a), TOP])), { size: 0.5, tone: "mid" }));
  return out.join("");
}

function setHand(v) {
  const a = angleOf(v);
  const tip = polar(R - 9, a);
  return (
    L(Iso.lineOnTop(polar(6, a), tip, FACE + 0.3, P), { tone: "red", className: "set-hand" }) +
    `<g class="iso-dots" data-tone="hi"><circle cx="${at([...tip, FACE + 0.3])[0].toFixed(1)}" cy="${at([...tip, FACE + 0.3])[1].toFixed(1)}" r="1.3" style="fill:var(--anatomy-red);opacity:1"/></g>`
  );
}

const needleD = (v) => {
  const a = angleOf(v);
  return Iso.lineOnTop(polar(-7, a), polar(R - 10, a), FACE + 0.6, P);
};

function hub() {
  return S(Iso.cylinder(C[0], C[1], 3.2, FACE, 1.6, P, 24), { tone: "hi", lit: true }) + D([at([C[0], C[1], FACE + 1.6])], { size: 0.6, tone: "hi" });
}

function knob() {
  const out = [];
  const [x, y] = KNOB;
  out.push(S(Iso.cylinder(x, y, 9, 0, 2, P, 32), { tone: "lo" }));
  out.push(S(Iso.cylinder(x, y, 7, 2, 7, P, 36, 0.5), { tone: "mid" }));
  out.push(L(Iso.knurl(x, y, 7, 2.6, 8.4, 40, P), { tone: "lo" }));
  // Its index points at the objective's setting.
  const a = angleOf(objective.value);
  out.push(L(Iso.lineOnTop([x, y], [x + Math.sin(a) * 6, y - Math.cos(a) * 6], 9, P), { tone: "hi" }));
  out.push(D([at([x, y, 9])], { size: 0.5, tone: "hi" }));
  const ticks = Iso.radialTicks(x, y, 9, 8, 4, 2, [0.8, 1.6], P, FROM, SPAN);
  out.push(L(ticks.minor, { tone: "lo" }), L(ticks.major, { tone: "mid" }));
  return out.join("");
}

function label() {
  const out = [S(Iso.slabOf(LABEL, 0, 0.8, P, 3, 0.3), { tone: "lo", crease: "none" })];
  for (const [i, frac] of [0.55, 0.8, 0.4].entries()) {
    const y = LABEL.y + 5 + i * 4.5;
    out.push(L(Iso.lineOnTop([LABEL.x + 4, y], [LABEL.x + 4 + (LABEL.w - 8) * frac, y], 0.8, P), { tone: "faint", free: true }));
  }
  for (const [x, y] of Iso.corners(LABEL, 2)) out.push(D([at([x, y, 0.8])], { size: 0.4 }));
  return out.join("");
}

// ---- assemble -------------------------------------------------------------
const svg = Iso.figureSvg({
  width: W,
  height: H,
  label: `A panel gauge on a bench plate, wired to a probe. Its red set hand marks the ${fmt(objective.value)} ${unit} objective; its needle reads checkout's p99, ${fmt(p99.value)} ${unit}.`,
  body: [
    plate(),
    probe(),
    cable(),
    gauge(),
    `<g data-part="set-hand">${setHand(objective.value)}</g>`,
    `<path data-part="needle" class="iso-line" data-tone="lit" data-free="start" d="${needleD(0)}" style="stroke-width:1.2"/>`,
    hub(),
    knob(),
    label(),
  ],
});

const root = cv.root;
root.replaceChildren();
Iso.mount(root);
root.innerHTML = Iso.plateHtml({
  fig: cfg.fig || "Fig 1",
  title: cfg.title || "p99",
  hint: "Replay to swing the needle",
  readout: "",
  keys: [
    { mark: "lit", label: "Needle: measured p99" },
    { mark: "red", label: "Set hand and red zone: the objective" },
  ],
  body: `<div class="iso-stage">${svg}</div>`,
});

root.querySelector(".zone").style.strokeWidth = "2.6";
const needle = root.querySelector('[data-part="needle"]');
const hand = root.querySelector('[data-part="set-hand"]');
cv.mark(needle, { evidence: p99, label: "p99", id: "p99" });
cv.mark(hand, { evidence: objective, label: "objective", id: "objective" });

// The plate's corners are aria-hidden; the figure's role="img" label carries
// both values for assistive tech.
const readout = root.querySelector("[data-readout]");
const piece = (text, ev, id) => {
  const s = document.createElement("span");
  s.textContent = text;
  if (ev) cv.mark(s, { evidence: ev, label: id, id: `${id}-readout` });
  readout.append(s);
  return s;
};
readout.textContent = "";
const nowPart = piece(`p99 ${fmt(p99.value)} ${unit}`, p99, "p99");
piece(" · ");
piece(`objective ${fmt(objective.value)} ${unit}`, objective, "objective");

// ---- motion: the needle eases to its value, then reports settled ----------
let shown = 0;
let frame = 0;
let finish = () => {};
function swingTo(v) {
  // A new step interrupts the swing in flight: stop it and settle its promise.
  cancelAnimationFrame(frame);
  finish();
  const still = document.documentElement.classList.contains("cv-reduced-motion");
  if (still) {
    shown = v;
    needle.setAttribute("d", needleD(v));
    return Promise.resolve();
  }
  return new Promise((resolve) => {
    finish = () => {
      finish = () => {};
      resolve();
    };
    let last = 0;
    const tick = (now) => {
      const dt = last ? Math.min((now - last) / 1000, 1 / 30) : 1 / 60;
      last = now;
      shown += (v - shown) * (1 - Math.exp(-dt / 0.16));
      if (Math.abs(v - shown) < FULL / 2000) shown = v;
      needle.setAttribute("d", needleD(shown));
      if (shown === v) finish();
      else frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
  });
}

cv.reveal({ steps: 2 }, (step) => {
  nowPart.style.opacity = step >= 1 ? "1" : "0.25";
  cv.highlight(step >= 1 ? ["p99"] : ["objective"]);
  return swingTo(step >= 1 ? p99.value : 0);
});
