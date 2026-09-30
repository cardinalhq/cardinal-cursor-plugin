#!/usr/bin/env python3
"""Validate migrated dashboards and alert rules in Cardinal.

For each item: (1) it exists in Cardinal (by the id cardinal_apply.py recorded),
and (2) every panel / alert query runs through lakerunner's native query API
(the one Cardinal's UI uses) and returns data. Dashboard variables are replaced
with `.+` ("All"). Each item ends with a RESULT line:
  PASS  in Cardinal, every query returns data
  WARN  in Cardinal, but some queries return no data or an error
  FAIL  not in Cardinal, or no query returns data

Usage:
  cardinal_verify.py --plan ./plan --catalog ./catalog [--dashboard <uid> ...] [--alert <name-or-number> ...]
                     [--window 1h] [--env-file .env.cardinal]

Writes plan/verify.json (merged across runs). Exits 1 if any item FAILs.
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_apply import load_applied, plan_alerts, plan_dashboards, select  # noqa: E402
from cardinal_catalog import Cardinal, NativeQuery, load_env_file, rule_states  # noqa: E402


def window_ms(w):
    n, u = int(w[:-1]), w[-1]
    return n * {"m": 60_000, "h": 3_600_000, "d": 86_400_000}[u]


def verdict(present, rows):
    ok = sum(1 for r in rows if r["points"] and not r["error"])
    if not present or (rows and ok == 0):
        return "FAIL"
    return "PASS" if ok == len(rows) else "WARN"


def report_rows(rows):
    ok = sum(1 for r in rows if r["points"] and not r["error"])
    mark = "✓" if ok == len(rows) else "⚠"
    print(f"  {mark} {ok}/{len(rows)} queries return data")
    for r in rows:
        if r["error"] or not r["points"]:
            print(f"    - {r['panel']}: {'ERROR ' + r['error'] if r['error'] else 'no data'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--catalog", required=True)
    ap.add_argument("--dashboard", action="append", help="dashboard uid or number from --list (repeatable)")
    ap.add_argument("--alert", action="append", help="alert rule name or number from --list (repeatable)")
    ap.add_argument("--window", default="1h")
    ap.add_argument("--env-file")
    args = ap.parse_args()
    load_env_file(args.env_file)

    dashboards, alerts = plan_dashboards(args.plan), plan_alerts(args.plan)
    picking = args.dashboard is not None or args.alert is not None
    dash_sel = select(dashboards, args.dashboard, lambda it: it[0], "dashboard") \
        if not picking or args.dashboard is not None else []
    alert_sel = select(alerts, args.alert, lambda it: it["name"], "alert rule") \
        if not picking or args.alert is not None else []

    # Queries need telemetry:query; the presence checks list dashboards / alert
    # rules, which only the matching write scope's routes admit.
    c = Cardinal.from_env(connect_scopes=["telemetry:query"]
                          + (["dashboards:write"] if dash_sel else [])
                          + (["alerts:write"] if alert_sel else []))
    lakes = json.load(open(os.path.join(args.catalog, "instance.json")))
    inst = lakes["chosen"]
    nq = NativeQuery(c, inst["id"], window_ms(args.window))
    applied = load_applied(args.plan)
    vpath = os.path.join(args.plan, "verify.json")
    try:
        results = json.load(open(vpath))
        assert isinstance(results.get("dashboards"), dict)
    except (OSError, ValueError, AssertionError, AttributeError):
        results = {"dashboards": {}, "alerts": {}}
    failed = 0

    if dash_sel:
        code, existing = c.req("GET", f"/api/orgs/{c.org}/dashboards")
        live = {d["id"] for d in existing} if code == 200 and isinstance(existing, list) else None
    for n, (uid, d) in dash_sel:
        entry = applied["dashboards"].get(uid, {})
        print(f"Validating dashboard {n}/{len(dashboards)} \"{entry.get('name', d['name'])}\"")
        present = bool(entry.get("id")) and (live is None or entry["id"] in live)
        if present:
            print(f"  ✓ present in Cardinal → {entry.get('url')}")
        else:
            print("  ✗ not in Cardinal" + ("" if entry.get("id") else " (not migrated yet)"))
        rows = []
        for p in d["spec"]["panels"].values():
            qs = [(q["query"], q.get("queryKind", "prometheus")) for q in p.get("queries", [])]
            if p.get("kind") == "log-events" and p.get("rawLogql"):
                qs = [(p["rawLogql"], "loki")]
            for expr, kind in qs:
                expr = re.sub(r"\$\{?\w+\}?", ".+", expr)
                points, err = nq.query("logs" if kind == "loki" else "metrics", expr)
                rows.append({"panel": p["title"], "query": expr, "points": points, "error": err})
        report_rows(rows)
        v = verdict(present, rows)
        failed += v == "FAIL"
        print(f"  RESULT: {v}")
        results["dashboards"][uid] = {"name": d["name"], "present": present, "result": v, "rows": rows,
                                      "queries": len(rows),
                                      "with_data": sum(1 for r in rows if r["points"] and not r["error"])}

    states_by_lake = {}
    for n, a in alert_sel:
        entry = applied["alerts"].get(a["name"], {})
        print(f"Validating alert rule {n}/{len(alerts)} \"{entry.get('name', a['name'])}\"")
        lake = next((i for i in lakes.get("all", [lakes["chosen"]]) if entry.get("instance") in (i["id"], i.get("slug"))), inst)
        slug = lake.get("slug") or lake["id"]
        if slug not in states_by_lake:
            states_by_lake[slug] = rule_states(c, slug)
        states = states_by_lake[slug]
        state = states.get(entry.get("id")) if states is not None else None
        present = bool(entry.get("id")) and (states is None or entry["id"] in states)
        want = entry.get("want_enabled", True)
        state_ok = state is None or state == want
        if not present:
            print("  ✗ not in Cardinal" + ("" if entry.get("id") else " (not migrated yet)"))
        else:
            shown = "state unknown" if state is None else "enabled" if state else "disabled"
            print(f"  {'✓' if state_ok else '✗'} present in Cardinal ({shown}, on data lake '{slug}')"
                  + ("" if state_ok else f" -- should be {'enabled' if want else 'disabled'}"))
        spec = a["rule_spec"]
        points, err = NativeQuery(c, lake["id"], window_ms(args.window)).query(spec["signal_type"], spec["query"]["expr"])
        rows = [{"panel": "rule query", "query": spec["query"]["expr"], "points": points, "error": err}]
        report_rows(rows)
        v = "FAIL" if present and not state_ok else verdict(present, rows)
        failed += v == "FAIL"
        print(f"  RESULT: {v}")
        results["alerts"][a["name"]] = {"present": present, "enabled": state, "result": v,
                                        "points": points, "error": err}

    json.dump(results, open(vpath, "w"), indent=2)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
