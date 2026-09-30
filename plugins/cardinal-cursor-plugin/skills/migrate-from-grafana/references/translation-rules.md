# Grafana → Cardinal translation rules

What `convert.py` does with each Grafana construct. "Adapted" means it still works
but looks or behaves differently; "skipped" means it is not created in Cardinal.
Every adaptation and skip is recorded in `plan/report.json`.

## Panels

| Grafana panel | Cardinal panel | Notes |
|---|---|---|
| timeseries, graph (legacy) | `timeseries` | stacking normal → `stacked-area` (or `stacked-bar` if bars), percent → `stacked-area`/`normalized-bar`, bars → `bar`; min/max → `yMin`/`yMax` |
| stat, singlestat | `stat` | reducer from `reduceOptions.calcs` (lastNotNull→last, mean, max, min, sum); first query only; sparkline unless `graphMode: none`; threshold colours dropped |
| gauge, bargauge | `stat` (adapted) | |
| piechart | `pie` | |
| barchart | `bar` | |
| table | `label` (adapted) | label/value list sorted by value |
| logs | `log-events` | first log query → `rawLogql` |
| heatmap, state-timeline, status-history, trend, xychart | `timeseries` (adapted) | |
| text, news, dashlist, alertlist, annolist | skipped | no equivalent |
| traces, nodeGraph, flamegraph, any Tempo/Jaeger/Zipkin/Pyroscope panel | skipped | use Cardinal's trace explorer |
| queries on other datasources (CloudWatch, SQL, Elasticsearch, …) | dropped | only Prometheus-compatible and Loki queries migrate |

Layout: Grafana rows become Cardinal sections (collapsed rows → `defaultCollapsed`);
panels before the first row form a section named "Panels". Grafana's 24-column
`gridPos` maps 1:1 to Cardinal's 24-column grid cells.

Units: `s ms bytes reqps ops Bps percent` etc. become Cardinal unit labels;
`percentunit` (0–1) becomes `ratio`. Cardinal's unit is a display label only.

## Variables

| Grafana variable | Cardinal |
|---|---|
| `label_values(metric{sel}, label)` on Prometheus | query variable, `signal: metrics`, `scope` from the selector |
| `label_values(label)` on Loki | query variable, `signal: logs` |
| custom, constant, interval, textbox, other query forms | **inlined**: the current value is substituted into queries (`All` → `.+`) |

Cardinal's "All" substitutes `.+`, so `label=~"$var"` keeps working.

## Queries

- Grafana macros: `$__rate_interval` → `5m`, `$__interval`/`$__auto` → `1m`, `$__range` → `1h`.
- Metric names are renamed via `mapping.json` (`_bucket/_sum/_count` follow their family).
  A metric mapped to `null` → the panel/rule is skipped ("metric not found in Cardinal").
- Label names are renamed in matchers, `by/without/on/ignoring` lists and legends.
- **Native histograms** (Cardinal has the histogram under its base name only, listed
  in `native_histograms`): `rate(M_count[w])` → `rate(M[w])` (request rate);
  `histogram_quantile(q, sum by (le, X)(rate(M_bucket[w])))` and
  `sum(rate(M_sum))/sum(rate(M_count))` → `max by (X) (M{sel})`, the histogram's own
  value, which is what Cardinal's built-in dashboards chart. Panels still titled
  "p95"/"p99" are flagged. Queries needing raw buckets (heatmaps) are skipped.
- `drop_labels`: matchers on labels/values absent from Cardinal are removed (an
  empty LogQL selector becomes `{service_name=~".+"}`).
- LogQL `... or vector(0)` is removed (Cardinal's log engine rejects it).
- `histogram_quantile(q, sum by (le, X) (rate(M_bucket[w])))` on classic histograms — if the instance
  doesn't support it, rewritten to `sum by (X)(rate(M_sum[w])) / sum by (X)(rate(M_count[w]))`
  (the **average**, not the quantile). Other `histogram_quantile` shapes are skipped.
- Loki: Grafana's derived `detected_level` → lakerunner's `level` label. Legends
  `{{label}}` keep working (same templating).
- If Cardinal's metric names contain dots (OTel-native names), plain PromQL can't
  reference them bare; check a converted query against Cardinal's query API and, if
  needed, use the `{"metric.name", label="x"}` selector form in the mapping target.

## Alert rules (Grafana-managed)

Single-query rules with one condition migrate to lakerunner `static_threshold` rules:

| Grafana | lakerunner `rule_spec` |
|---|---|
| data query (Prometheus / Loki) | `signal_type` metrics / logs, `query.expr` (translated), `query.range_seconds` from `relativeTimeRange.from` |
| reduce → threshold (or classic condition, or `$B > x` math) | `detection: {kind: static_threshold, reducer, operator, threshold}` (mean→avg; count→sum, flagged) |
| group interval | `eval_interval_seconds` (min 15) |
| `for` | `for_duration_seconds` |
| labels | `labels` |
| annotations | `annotations` (Go templates → plain text, `{{ $labels.x }}` → `<x>`); `description` from description/summary |
| paused | created, then disabled |

Skipped: multi-query rules, multi-condition classic rules, range thresholds
(`within_range`/`outside_range`), complex math, recording rules, and
datasource-managed (Prometheus/Loki ruler) rules — those live in Mimir/Loki, not
Grafana's ruler API; export them from the datasource and recreate by hand if needed.

Not carried over (tell the user):
- `noDataState: Alerting` — lakerunner doesn't fire on missing data. A "traffic
  stopped" alert should be recreated as a Cardinal *anomaly* rule with `detect_sparse`.
- Contact points, notification policies, mute timings, silences — set up Cardinal
  notification groups and attach them to the rules.
- Per-rule `execErrState`.
