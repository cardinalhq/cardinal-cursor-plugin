// Exemplar 1 of 3: the smallest complete surface. Three marked numbers above a
// line, two reveal steps. Read it for the shape of a surface, not the look.
//
// Bindings this surface expects (define_surface bindings or the scene's own):
//   p99       {receiptId, selector: "/data_points/0/metrics/value", unit: "ms"}  a scalar
//   baseline  {receiptId, selector: "…", unit: "ms"}                              a scalar
//   change    {derive: {op: "percent_change", left: <baseline source>, right: <p99 source>}}
//             maestro computes the percent; the frame never does
//   series    {receiptId, selector: "/data_points"}   rows {groupBy: {timestamp}, metrics: {<one aggregation>}}
//
// The batch form of cv.data resolves to an OBJECT keyed by binding key, so
// destructure it with {…}. Array destructuring throws "is not iterable".
const { p99, baseline, change, series } = await cv.data(["p99", "baseline", "change", "series"]);
const root = cv.root;
root.replaceChildren();

const head = document.createElement("div");
head.style.cssText = "display:flex;gap:24px;font:var(--cv-text-lg) var(--cv-font-sans)";
for (const [ev, label] of [[p99, "p99 now"], [baseline, "p99 before"], [change, "change"]]) {
  const cell = document.createElement("div");
  // Formatting a bound value is fine; inventing or computing one is not.
  cell.textContent = label + ": " + Math.round(ev.value) + (ev.unit === "%" ? "%" : " " + (ev.unit || ""));
  cv.mark(cell, { evidence: ev, label, id: label.replace(/\W+/g, "-") });
  head.append(cell);
}
root.append(head);

const W = 900, H = 220;
const svg = d3.select(root).append("svg").attr("viewBox", `0 0 ${W} ${H}`).attr("width", W);
const rows = series.value;
const val = (d) => Object.values(d.metrics)[0]; // the row's one aggregation (sum, avg, …)
const x = d3.scaleUtc().domain(d3.extent(rows, (d) => new Date(d.groupBy.timestamp))).range([40, W - 10]);
const y = d3.scaleLinear().domain([0, d3.max(rows, val)]).nice().range([H - 20, 10]);
svg.append("g").attr("data-cv-axis", "").attr("transform", `translate(0,${H - 20})`).call(d3.axisBottom(x).ticks(6));
const line = svg.append("path").attr("fill", "none").attr("stroke", "var(--cv-series-1)").attr("stroke-width", 2)
  .attr("d", d3.line((d) => x(new Date(d.groupBy.timestamp)), (d) => y(val(d)))(rows));
line.attr("data-cv-anchor", "series");
cv.mark(line.node(), { evidence: series, label: "the series" });

cv.reveal({ steps: 2 }, (step) => {
  line.style("opacity", step >= 1 ? 1 : 0.15);
  cv.highlight(step >= 1 ? ["series"] : []);
});
