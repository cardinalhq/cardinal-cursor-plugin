// Exemplar 3 of 3: put the population on the page, one row per member, so
// the viewer can see the claim rather than trust it. Two groups of checkouts
// that both reached the customer as a 502: payment declines, and a cohort
// whose call to payment got no HTTP answer at all (EOF). The dominant mark is
// what separates them; duration is drawn small on one shared scale, because
// it does NOT separate them. From the conductor spike's hidden-cohort fixture.
// One SVG row (and one cv.mark) per member suits tens of rows. Past a few
// thousand, paint the dataset onto a <canvas> and cv.mark the canvas once,
// or bind a reduce/derive and draw the summary.
//
// Surface libraries: ["d3"].
// Bindings. Rows are numbered, and the surface probes until a key is missing,
// so the scene decides how many rows there are:
//   dec<n>Srv   {receiptId, selector: "/spans/<n>"}  cart's server span for decline n
//   dec<n>Cli   {receiptId, selector: "/spans/<n>"}  cart's client call to payment
//   dec<n>Dur   {receiptId, selector: "/spans/<n>/tags/duration", unit: "ns"}
//   eof<i>      {receiptId, selector: "/spans/<i>"}  the EOF call
//   body<i>     {receiptId, selector: "/spans/<i>"}  the customer response it caused
//   eof<i>Dur   {receiptId, selector: "/spans/<i>/tags/duration", unit: "ns"}
//   eofCount    {derive: {op: "count", left: {receiptId, selector: "/spans"}}}  the WHOLE population
//   bodyCount   {derive: {op: "count", left: {receiptId, selector: "/spans"}}}
// Positional selectors (/spans/3) need `expect` guards on the identifying
// fields (trace_id, status), or a reordered result silently points at the
// wrong row. The counts are derived from whole receipts, so the sentence at
// the bottom cannot drift from the rows above it.

const NS = "http://www.w3.org/2000/svg";
const fmtClock = d3.utcFormat("%H:%M:%S");
const fmtMs = (ns) => `${d3.format(".1f")(ns / 1e6)} ms`; // the bound unit is ns; this only formats it

function svgEl(tag, attrs, text) {
  const e = document.createElementNS(NS, tag);
  if (tag === "text") e.setAttribute("style", "paint-order:stroke;stroke:var(--cv-bg);stroke-width:4px;stroke-linejoin:round");
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
  if (text !== undefined) e.textContent = text;
  return e;
}
async function maybe(key) {
  try { return await cv.data(key); } catch { return null; } // an unbound key rejects
}

const { eofCount, bodyCount } = await cv.data(["eofCount", "bodyCount"]);
const declines = [];
for (let n = 0; ; n++) {
  const srv = await maybe(`dec${n}Srv`);
  if (!srv) break;
  const { [`dec${n}Cli`]: cli, [`dec${n}Dur`]: dur } = await cv.data([`dec${n}Cli`, `dec${n}Dur`]);
  declines.push({ srv, cli, dur });
}
const cohort = [];
for (let i = 0; ; i++) {
  const eof = await maybe(`eof${i}`);
  if (!eof) break;
  const { [`body${i}`]: body, [`eof${i}Dur`]: dur } = await cv.data([`body${i}`, `eof${i}Dur`]);
  cohort.push({ eof, body, dur });
}
cohort.sort((a, b) => a.eof.value.timestamp - b.eof.value.timestamp);

const W = 1248, rowH = 38, y0 = 150, gap = 56;
const H = y0 + (declines.length + cohort.length) * rowH + gap + 110;
const svg = svgEl("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` });
cv.root.append(svg);

// The headline states the finding; the rows below are its evidence.
svg.append(svgEl("text", { x: 0, y: 34, "font-size": 30, "font-weight": 700, fill: "var(--cv-fg)" }, "Not a decline."));
svg.append(svgEl("text", { x: 0, y: 62, "font-size": 16, fill: "var(--cv-muted-fg)" },
  "Both reach the customer as a 502. What separates them is whether cart's call to payment got any HTTP answer at all."));

const C = { time: 0, answer: 190, dur0: 560, dur1: 820, cust: 900, trace: 1070 };
// One shared duration scale, from the bound values (layout, not a claim).
const maxDur = d3.max([...declines, ...cohort], (r) => r.dur.value) || 1;
const dx = d3.scaleLinear().domain([0, maxDur]).nice().range([C.dur0, C.dur1]);
const hy = 112;
const hdr = svgEl("g", { "data-cv-axis": "" });
svg.append(hdr);
for (const [x, t] of [[C.time, "checkout (UTC)"], [C.answer, "what cart's call to payment got back"],
  [C.dur0, "call duration (one scale)"], [C.cust, "customer got"], [C.trace, "trace"]]) {
  hdr.append(svgEl("text", { x, y: hy, "font-size": 12, "font-weight": 650, fill: "var(--cv-muted-fg)" }, t));
}
hdr.append(svgEl("line", { x1: 0, x2: W, y1: hy + 10, y2: hy + 10, stroke: "var(--cv-border)" }));

function durBar(y, ev, emph, id) {
  svg.append(svgEl("line", { x1: C.dur0, x2: C.dur1, y1: y, y2: y, stroke: "var(--cv-border)", "stroke-dasharray": "2 3" }));
  svg.append(svgEl("rect", { x: C.dur0, y: y - 4, width: Math.max(2, dx(ev.value) - C.dur0), height: 8, rx: 2,
    fill: emph ? "var(--cv-bad)" : "var(--cv-warn)", "fill-opacity": 0.45 }));
  const t = svgEl("text", { x: Math.max(dx(ev.value), C.dur0 + 2) + 6, y: y + 4, "font-size": 11, fill: "var(--cv-muted-fg)" }, fmtMs(ev.value));
  svg.append(t);
  cv.mark(t, { evidence: ev, label: "call duration", id });
}
function chip(x, y, text, eofKind) {
  const g = svgEl("g", {});
  const w = eofKind ? 300 : 120;
  g.append(svgEl("rect", { x, y: y - 14, width: w, height: 28, rx: 14,
    fill: eofKind ? "var(--cv-bad)" : "none", stroke: eofKind ? "var(--cv-bad)" : "var(--cv-warn)", "stroke-width": 2 }));
  const t = svgEl("text", { x: x + w / 2, y: y + 5, "text-anchor": "middle", "font-size": 14, "font-weight": 700,
    fill: eofKind ? "#fff" : "var(--cv-warn)" }, text);
  t.setAttribute("style", "");
  g.append(t);
  svg.append(g);
  return g;
}
function customer(y, span, color) {
  const tags = span.value.tags;
  svg.append(svgEl("text", { x: C.cust, y: y + 5, "font-size": 15, "font-weight": 700, fill: "var(--cv-fg)" }, `${tags.http_response_status_code}`));
  const b = svgEl("text", { x: C.cust + 42, y: y + 5, "font-size": 14, "font-weight": color ? 700 : 400,
    fill: color || "var(--cv-muted-fg)" }, `${tags.http_response_body_size} B body`);
  svg.append(b);
  return b;
}
const trace8 = (t) => String(t).slice(0, 8);

let y = y0;
svg.append(svgEl("text", { x: 0, y: y - 8, "font-size": 13, "font-weight": 650, fill: "var(--cv-warn)" }, "declines"));
y += 16;
declines.forEach((d, n) => {
  const cy = y + n * rowH;
  svg.append(svgEl("text", { x: C.time, y: cy + 5, "font-size": 14, fill: "var(--cv-fg)" }, fmtClock(new Date(d.srv.value.timestamp))));
  const g = chip(C.answer, cy, `HTTP ${d.cli.value.tags.http_response_status_code}`, false);
  cv.mark(g, { evidence: d.cli, label: "cart's call to payment: HTTP answer", id: `dec-ans-${n}` });
  durBar(cy, d.dur, false, `dec-dur-${n}`);
  cv.mark(customer(cy, d.srv), { evidence: d.srv, label: "cart's response to the customer", id: `dec-resp-${n}` });
  svg.append(svgEl("text", { x: C.trace, y: cy + 5, "font-size": 12, "font-family": "var(--cv-font-mono)", fill: "var(--cv-muted-fg)" },
    trace8(d.srv.value.tags.trace_id)));
});

y = y0 + 16 + declines.length * rowH + gap;
svg.append(svgEl("text", { x: 0, y: y - 8, "font-size": 13, "font-weight": 650, fill: "var(--cv-bad)" }, "the EOF cohort"));
y += 16;
cohort.forEach((c, i) => {
  const cy = y + i * rowH;
  svg.append(svgEl("text", { x: C.time, y: cy + 5, "font-size": 14, "font-weight": 650, fill: "var(--cv-fg)" }, fmtClock(new Date(c.eof.value.timestamp))));
  const g = chip(C.answer, cy, `no HTTP status · "${c.eof.value.tags.status_message}"`, true);
  cv.mark(g, { evidence: c.eof, label: "cart's call to payment: no HTTP status", id: `eof-ans-${i}` });
  durBar(cy, c.dur, true, `eof-dur-${i}`);
  cv.mark(customer(cy, c.body, "var(--cv-bad)"), { evidence: c.body, label: "cart's response to the customer", id: `eof-resp-${i}` });
  // The join, made visible: the call's trace equals the response's trace.
  const tt = svgEl("text", { x: C.trace, y: cy + 5, "font-size": 12, "font-family": "var(--cv-font-mono)", fill: "var(--cv-muted-fg)" });
  tt.append(trace8(c.eof.value.tags.trace_id), " = ", trace8(c.body.value.tags.trace_id));
  svg.append(tt);
});

// Population sentence: both counts are derived over whole receipts and marked.
const sy = y + cohort.length * rowH + 30;
const s = svgEl("text", { x: 0, y: sy, "font-size": 14, fill: "var(--cv-fg)" });
const n1 = svgEl("tspan", { "font-weight": 700, fill: "var(--cv-bad)" }, `${eofCount.value}`);
const n2 = svgEl("tspan", { "font-weight": 700, fill: "var(--cv-bad)" }, `${bodyCount.value}`);
s.append(n1, " EOF calls in the window came back with no HTTP status, and the window has ", n2,
  " checkout 502s with that response body: the rows above, trace for trace.");
svg.append(s);
cv.mark(n1, { evidence: eofCount, label: "EOF payment calls in the window", id: "eofCount" });
cv.mark(n2, { evidence: bodyCount, label: "checkout 502s with the EOF response body in the window", id: "bodyCount" });
