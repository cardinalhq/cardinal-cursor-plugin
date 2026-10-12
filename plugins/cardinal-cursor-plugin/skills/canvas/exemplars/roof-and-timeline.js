// Exemplar 2 of 3: draw the physical thing, then compose a prefab under it.
// A roof with one panel per microinverter, coloured by production, above the
// site power timeline prefab. A callout ties the dead panel to the outage
// window, and one surface is shared by two scenes (cv.onUpdate).
// From the conductor spike's solar-inverter-outage fixture.
//
// Surface libraries: ["d3", "prefabs", "icons"]. "prefabs" is what makes
// cv.embed available.
// Bindings:
//   panels   {receiptId, selector: "/data_points", unit: "W"}  rows {groupBy: {inverter_serial}, metrics: {avg}}
//   site     {receiptId, selector: "/data_points"}             the site power series (timeline prefab input)
//   focus    {receiptId, selector: "/data_points/0/groupBy/inverter_serial"}  the lowest producer, by the query
//   before   {receiptId, selector: "/ddsketches/inverter_serial=<serial>/avg", unit: "W"}
//   after    same shape, from the later window's receipt
//   change   {derive: {op: "percent_change", left: <before source>, right: <after source>}}
//   outageStart / outageEnd  {receiptId, selector: "/data_points/<i>/groupBy/timestamp"}  first
//            scene only (config.showOutage). These are positional selectors, so give them
//            `expect` guards on the identifying fields.
// Config (presentational only): {heading, showOutage, windowLabel}.
//
// Reveal: 0 = the roof, uncoloured · 1 = production + marked numbers ·
//         2 = focus the offline inverter + outage window, with a callout.

const NS = "http://www.w3.org/2000/svg";
const fmtW = d3.format(",.0f");

async function draw() {
  const cfg = cv.config || {};
  const { panels, site, focus, before, after, change } =
    await cv.data(["panels", "site", "focus", "before", "after", "change"]);

  const root = cv.root;
  root.replaceChildren();
  root.style.position = "relative";

  const head = document.createElement("div");
  head.style.cssText = "display:flex;align-items:baseline;gap:12px;margin-bottom:8px";
  const h = document.createElement("div");
  h.textContent = cfg.heading || "";
  h.style.cssText = "font-size:var(--cv-text-lg);font-weight:600";
  const sub = document.createElement("div");
  sub.style.cssText = "color:var(--cv-muted-fg)";
  sub.innerHTML = cv.icons.sun.replace('width="24" height="24"', 'width="16" height="16"');
  sub.append(" avg power per microinverter in the window");
  head.append(h, sub);
  root.append(head);

  const W = 1200, H = 300;
  const svg = d3.select(root).append("svg").attr("width", W).attr("height", H).attr("viewBox", `0 0 ${W} ${H}`);

  // The house is chrome: tag it so it is not mistaken for data.
  const house = svg.append("g").attr("data-cv-axis", "");
  house.append("path").attr("d", "M70 170 L70 290 L610 290 L610 170").attr("fill", "var(--cv-surface)")
    .attr("stroke", "var(--cv-border)").attr("stroke-width", 2);
  house.append("path").attr("d", "M40 176 L150 26 L530 26 L640 176 Z").attr("fill", "var(--cv-muted)")
    .attr("stroke", "var(--cv-border)").attr("stroke-width", 2);

  // One panel per bound row. Status goes on the object, not in a legend.
  const rows = panels.value;
  const max = d3.max(rows, (d) => d.metrics.avg) || 1;
  const color = d3.scaleLinear().domain([0, max * 0.5, max]).range(["#7f1d1d", "#b45309", "#fbbf24"]);
  const cols = 6, pw = 62, ph = 52;
  const cells = svg.append("g").selectAll("g.panel").data(rows).join("g").attr("class", "panel")
    .attr("transform", (d, i) => {
      const c = i % cols, r = Math.floor(i / cols);
      return `translate(${150 + c * (pw + 8) - r * 22}, ${44 + r * (ph + 12)}) skewX(-20)`;
    });
  const rects = cells.append("rect").attr("width", pw).attr("height", ph).attr("rx", 3)
    .attr("stroke", "var(--cv-bg)").attr("stroke-width", 2).attr("fill", "var(--cv-border)");
  const labels = cells.append("text").attr("x", pw / 2).attr("y", ph / 2 + 5).attr("text-anchor", "middle")
    .attr("font-size", 13).attr("font-weight", 600).attr("fill", "#fff").style("opacity", 0)
    .text((d) => fmtW(d.metrics.avg));
  labels.each(function (d, i) {
    cv.mark(this, {
      evidence: panels.at(i), // one row of a bound array is its own evidence
      label: `avg power, inverter ${d.groupBy.inverter_serial}`,
      id: d.groupBy.inverter_serial === focus.value ? "panel-focus" : `panel-${i}`,
    });
  });

  // Before / after / change: every number is a binding, the change is derived.
  const side = svg.append("g").attr("transform", "translate(700, 60)");
  side.append("text").attr("font-size", 13).attr("fill", "var(--cv-muted-fg)").text(`inverter ${focus.value}`);
  const line = (y, label, ev, fmt, id) => {
    side.append("text").attr("y", y).attr("font-size", 13).attr("fill", "var(--cv-muted-fg)").text(label);
    const t = side.append("text").attr("x", 200).attr("y", y).attr("font-size", 20).attr("font-weight", 600)
      .attr("fill", "var(--cv-fg)").text(fmt(ev));
    cv.mark(t.node(), { evidence: ev, label, id });
    return t;
  };
  line(40, "avg before", before, (e) => `${fmtW(e.value)} W`, "before");
  line(76, "avg after", after, (e) => `${fmtW(e.value)} W`, "after");
  line(112, "change", change, (e) => `${d3.format("+.0f")(e.value)}%`, "change")
    .attr("fill", change.value < 0 ? "var(--cv-bad)" : "var(--cv-good)");
  side.style("opacity", 0);

  // The window's edges are measured timestamps: bindings, passed to the
  // prefab AS EVIDENCE (cv.embed rejects plain numbers in data-bearing props).
  const windows = [];
  if (cfg.showOutage) {
    const { outageStart, outageEnd } = await cv.data(["outageStart", "outageEnd"]);
    windows.push({ id: "outage", start: outageStart, end: outageEnd, label: cfg.windowLabel || "", color: "#dc2626" });
  }
  const tlBox = document.createElement("div");
  tlBox.style.marginTop = "8px";
  root.append(tlBox);
  const tl = cv.embed("timeline",
    { series: site, windows, unit: "W", yAxisLabel: "site power", height: 230, showLegend: false },
    { id: "tl" });
  tlBox.append(tl.el);

  // Callout from the dead panel to the prefab's named anchor "window:outage".
  const overlay = document.createElementNS(NS, "svg");
  overlay.style.cssText = "position:absolute;left:0;top:0;width:100%;height:100%;pointer-events:none;overflow:visible";
  root.append(overlay);
  let step = 0;
  const drawCallout = () => {
    overlay.replaceChildren();
    if (step < 2 || !windows.length) return;
    const target = tl.anchor("window:outage");
    const focusEl = root.querySelector('[data-cv-mark="panel-focus"]');
    if (!target || !focusEl) return;
    const rr = root.getBoundingClientRect();
    const ox = rr.left + window.scrollX, oy = rr.top + window.scrollY;
    const f = focusEl.getBoundingClientRect();
    const x1 = f.left + window.scrollX + f.width / 2 - ox, y1 = f.bottom + window.scrollY - oy + 6;
    const x2 = target.x + target.width / 2 - ox, y2 = target.y - oy + 4;
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", `M${x1},${y1} C${x1},${(y1 + y2) / 2} ${x2},${(y1 + y2) / 2} ${x2},${y2}`);
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", "var(--cv-bad)");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("stroke-dasharray", "5 4");
    overlay.append(path);
  };
  tl.on("layout", drawCallout); // prefabs lay out asynchronously

  cv.reveal({ steps: 3 }, (s) => {
    step = s;
    rects.attr("fill", s === 0 ? "var(--cv-border)" : (d) => color(d.metrics.avg));
    labels.style("opacity", s >= 1 ? 1 : 0);
    side.style("opacity", s >= 1 ? 1 : 0);
    // Marks, and prefab anchors as "<embedId>:<anchor>".
    cv.highlight(s >= 2 ? ["panel-focus", "before", "after", "change", ...(windows.length ? ["tl:window:outage"] : [])] : [],
      { mode: "dim-others" });
    drawCallout();
  });
}

await draw();
cv.onUpdate(draw); // the next scene on this surface: same frame, new bindings
