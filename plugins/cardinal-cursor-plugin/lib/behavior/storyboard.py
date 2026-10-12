# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""The proven behavior_stream Storyboard HTML, with a deployed receipt loader.

The HTML rendering body is reused from the local Step 8 prototype. This module
only renders durable results; it does not evaluate a behavior.
"""
from html import escape
import json
from pathlib import Path
from types import SimpleNamespace


def render_storyboard(state: dict, definition: dict, preview_data: dict, output: Path) -> dict:
    receipt = state["execution_id"]
    if state["status"] != "COMPLETED" or state.get("receipt") != receipt:
        raise ValueError("Storyboard requires a completed execution receipt")
    if state["diagnostic_version"] != definition["version"]:
        raise ValueError("receipt does not belong to this behavior definition")
    counts = state["counts"]
    results = []
    for raw in state["results"]:
        observations = raw.get("validation_observations") or {}
        results.append(SimpleNamespace(
            trace_id=raw["trace_id"], verdict=raw["verdict"],
            witness_refs=raw.get("witness_refs", []), records=raw.get("records", []),
            jev_receipts=raw.get("jev_receipts", []),
            coverage_gaps=raw.get("coverage_gaps", observations.get("coverage_gaps", [])),
            source_events=raw.get("semantic_occurrences", []),
            native_receipt=(raw.get("native_trace") or {}).get("receipt"),
            error=raw.get("error") or (raw.get("reason", "") if raw["verdict"] == "ERROR" else "")))
    report = SimpleNamespace(results=results, population_size=counts["population"],
        matches=counts["MATCH"], non_matches=counts["NON_MATCH"],
        unknown=counts["UNKNOWN"], errors=counts["ERROR"],
        population_specification=state["population_specification"])
    definition = SimpleNamespace(**definition)
    preview = SimpleNamespace(**preview_data)
    clauses = [item["interpretation"] for item in preview.clauses]
    trigger = next((item["interpretation"] for item in preview.clauses
                    if item["kind"] == "trigger"), "Accepted Behavioral Program")

    def code(value: object) -> str:
        return escape(json.dumps(value, indent=2, ensure_ascii=False))

    scene_cards = []
    labels = {"MATCH": "Observed matches", "NON_MATCH": "Observed non-matches",
              "UNKNOWN": "Unresolved traces", "ERROR": "Execution errors"}
    for verdict, label in labels.items():
        cases = [(index, item) for index, item in enumerate(report.results)
                 if item.verdict == verdict]
        if not cases:
            continue
        rows = []
        for index, item in cases:
            anchor = f"trace-{index + 1}"
            if verdict == "MATCH":
                violation = next((record.get("reason", "") for record in item.records
                                  if record.get("op") == "violation"), "")
                finding = violation or "The accepted program returned MATCH."
            elif verdict == "UNKNOWN":
                finding = "; ".join(str(gap.get("reason", gap))
                                    for gap in item.coverage_gaps) or "Evidence is incomplete."
            elif verdict == "ERROR":
                finding = item.error or "Trace evaluation failed."
            else:
                finding = "The accepted program returned NON_MATCH."
            summary = (f"{escape(item.trace_id)} · {len(item.witness_refs)} witnesses · "
                       f"{len(item.records)} Recorder records · {len(item.jev_receipts)} JEV receipts")
            rows.append(f'<li><a href="#{anchor}">{summary}</a><br>'
                        f'{escape(finding)}</li>')
        scene_cards.append(f'<section class="scene"><h2>{label} ({len(cases)})</h2>'
                           f'<p>Each trace below links to its committed result in receipt '
                           f'<code>{escape(receipt)}</code>.</p><ul>{"".join(rows)}</ul></section>')

    evidence = []
    for index, item in enumerate(report.results):
        source = (f'<h4>Retained source events</h4><pre>{code(item.source_events)}</pre>'
                  f'<h4>Native materialization receipt</h4><pre>{code(item.native_receipt)}</pre>'
                  if item.source_events or item.native_receipt else '')
        evidence.append(
            f'<details id="trace-{index + 1}"><summary>{escape(item.verdict)} · '
            f'{escape(item.trace_id)}</summary><p>Receipt <code>{escape(receipt)}</code>, '
            f'result {index + 1} of {report.population_size}</p>'
            f'<h4>Witness refs</h4><pre>{code(item.witness_refs)}</pre>'
            f'<h4>Recorder records</h4><pre>{code(item.records)}</pre>'
            f'<h4>JEV receipts</h4><pre>{code(item.jev_receipts)}</pre>'
            f'{source}'
            f'<h4>Coverage gaps</h4><pre>{code(item.coverage_gaps)}</pre>'
            f'<h4>Execution error</h4><pre>{code(item.error)}</pre></details>')
    html = f'''<!doctype html><html lang="en"><meta charset="utf-8">
<title>Behavior Storyboard · {escape(definition.diagnostic_id)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:980px;margin:3rem auto;padding:0 1.5rem;color:#182236;background:#f7f9fc}}
h1{{font-size:2.4rem}} .lede{{font-size:1.2rem}} .scenes{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}}
.scene,details{{background:white;border:1px solid #dce3ef;border-radius:12px;padding:1.25rem;margin:1rem 0;box-shadow:0 2px 8px #17223b0a}}
pre{{overflow:auto;background:#f1f4f9;padding:1rem;border-radius:8px;font-size:.8rem}} code{{overflow-wrap:anywhere}} a{{color:#1454a2}} li{{margin:.4rem 0}}
</style><main><p>Receipt-backed Behavioral Storyboard</p><h1>{escape(definition.diagnostic_id)}</h1>
<p class="lede">Across {report.population_size} traces: {report.matches} match, {report.non_matches} do not match, {report.unknown} remain unknown, and {report.errors} had execution errors.</p>
<p>{escape(trigger)}</p>
<p><strong>Accepted definition:</strong> <code>{escape(definition.version)}</code><br>
<strong>Complete receipt:</strong> <code>{escape(receipt)}</code><br>
<strong>Population:</strong> <code>{code(report.population_specification)}</code></p>
<h2>Behavior definition</h2><ol>{''.join(f'<li>{escape(clause)}</li>' for clause in clauses)}</ol>
<div class="scenes">{''.join(scene_cards)}</div><h2>Trace evidence</h2>{''.join(evidence)}
</main></html>'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html)
    return {"storyboard": str(output), "receipt": receipt,
            "population_size": report.population_size,
            "verdicts": {name: sum(item.verdict == name for item in report.results)
                         for name in labels}}
