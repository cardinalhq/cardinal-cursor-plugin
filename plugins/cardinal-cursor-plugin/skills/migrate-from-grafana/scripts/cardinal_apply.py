#!/usr/bin/env python3
"""Create (or update) the converted dashboards and alert rules in Cardinal.

Dry run by default: prints what would be created/updated. Pass --apply to write.
Idempotent: a dashboard or alert rule with the same name is updated in place,
so re-running after fixing the mapping does not create duplicates.

The skill migrates one item at a time (--dashboard / --alert) and validates each
with cardinal_verify.py before moving on; --list prints the plan in that order.

Reads CARDINAL_TOKEN or CARDINAL_API_KEY (env or --env-file); without either it
uses the cardinal-connect token when that was connected with dashboards:write /
alerts:write (whichever this run writes). CARDINAL_URL and CARDINAL_ORG_ID default
to the cardinal-connect org.

Usage:
  cardinal_apply.py --plan ./plan --list
  cardinal_apply.py --plan ./plan --catalog ./catalog [--apply] [--only dashboards|alerts]
                    [--dashboard <uid> ...] [--alert <name-or-number> ...]
                    [--instance <data-lake>] [--disable-alerts] [--name-prefix "..."] [--env-file .env.cardinal]

Writes plan/applied.json (merged across runs) with the Cardinal id + URL of
everything created/updated, keyed by dashboard uid / alert name.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cardinal_catalog import Cardinal, load_env_file, set_rule_enabled  # noqa: E402


def plan_dashboards(plan):
    """[(uid, {"name", "spec"})] in migration order."""
    ddir = os.path.join(plan, "dashboards")
    files = sorted(f for f in os.listdir(ddir) if f.endswith(".json")) if os.path.isdir(ddir) else []
    return [(f[:-5], json.load(open(os.path.join(ddir, f)))) for f in files]


def plan_alerts(plan):
    path = os.path.join(plan, "alerts.json")
    return json.load(open(path)) if os.path.exists(path) else []


def select(items, wanted, key, label):
    """Filter (number, item) pairs by uid/name or 1-based number; exit on an unknown one."""
    numbered = list(enumerate(items, 1))
    if wanted is None:
        return numbered
    out = []
    for w in wanted:
        hit = [(n, it) for n, it in numbered if w in (key(it), str(n))]
        if not hit:
            sys.exit(f"{label} '{w}' is not in the plan; see --list")
        out += [h for h in hit if h not in out]
    return out


def panels(n):
    return f"{n} panel" + ("" if n == 1 else "s")


def load_applied(plan):
    path = os.path.join(plan, "applied.json")
    try:
        data = json.load(open(path))
        if isinstance(data.get("dashboards"), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"dashboards": {}, "alerts": {}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", required=True)
    ap.add_argument("--catalog", help="dir written by cardinal_catalog.py (for the instance id)")
    ap.add_argument("--list", action="store_true", help="print the plan in migration order and exit")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", choices=["dashboards", "alerts"])
    ap.add_argument("--dashboard", action="append", help="dashboard uid or number from --list (repeatable)")
    ap.add_argument("--alert", action="append", help="alert rule name or number from --list (repeatable)")
    ap.add_argument("--name-prefix", default="")
    ap.add_argument("--instance", help="data lake (slug or id from catalog/instance.json) for alert rules, "
                    "e.g. when the default one has alerting switched off")
    ap.add_argument("--disable-alerts", action="store_true",
                    help="create/update alert rules switched off (rules paused in Grafana always are)")
    ap.add_argument("--env-file")
    args = ap.parse_args()

    dashboards, alerts = plan_dashboards(args.plan), plan_alerts(args.plan)
    if args.list:
        print(f"Dashboards ({len(dashboards)}):")
        for n, (uid, d) in enumerate(dashboards, 1):
            print(f"  {n}. {uid}  \"{args.name_prefix}{d['name']}\"  ({panels(len(d['spec']['panels']))})")
        print(f"Alert rules ({len(alerts)}):")
        for n, a in enumerate(alerts, 1):
            print(f"  {n}. \"{args.name_prefix}{a['name']}\"")
        return
    if not args.catalog:
        ap.error("--catalog is required")

    # --dashboard / --alert pick items; naming only one kind leaves the other out.
    picking = args.dashboard is not None or args.alert is not None
    do_dash = args.only != "alerts" and (not picking or args.dashboard is not None)
    do_alerts = args.only != "dashboards" and (not picking or args.alert is not None)
    dash_sel = select(dashboards, args.dashboard, lambda it: it[0], "dashboard") if do_dash else []
    alert_sel = select(alerts, args.alert, lambda it: it["name"], "alert rule") if do_alerts else []

    load_env_file(args.env_file)
    c = Cardinal.from_env(connect_scopes=[s for s, on in (("dashboards:write", bool(dash_sel)),
                                                          ("alerts:write", bool(alert_sel))) if on])
    org = c.org
    lakes = json.load(open(os.path.join(args.catalog, "instance.json")))
    instance = lakes["chosen"]
    if args.instance:
        match = [i for i in lakes.get("all", [lakes["chosen"]]) if args.instance in (i["id"], i.get("slug"), i.get("name"))]
        if not match:
            sys.exit(f"data lake '{args.instance}' not found; available: {[i.get('slug') for i in lakes.get('all', [lakes['chosen']])]}")
        instance = match[0]
    applied = load_applied(args.plan)
    dry = {"mode": "DRY RUN", "dashboards": [], "alerts": []}
    failed = 0

    def done(verb, kind, name, detail, entry):
        nonlocal failed
        if not args.apply:
            print(f"  would {verb} {kind} \"{name}\" {detail}".rstrip())
        elif entry.get("error"):
            failed += 1
            print(f"  ✗ could not {verb} {kind} \"{name}\": HTTP {entry['status']} {entry['error']}")
        else:
            mark = "⚠" if "FAILED" in detail else "✓"
            print(f"  {mark} {verb}d {kind} \"{name}\" {detail}".rstrip() + (f" → {entry['url']}" if entry.get("url") else ""))

    if dash_sel:
        code, existing = c.req("GET", f"/api/orgs/{org}/dashboards")
        if code != 200:
            sys.exit(f"cannot list Cardinal dashboards ({code}): {existing}. Writing dashboards needs a "
                     "Member/Owner of the org: a login token (CARDINAL_TOKEN), cardinal-connect with "
                     "dashboards:write, or an org API key with admin:all scope.")
        by_name = {d["name"]: d for d in existing}
        for n, (uid, d) in dash_sel:
            name = args.name_prefix + d["name"]
            prior = by_name.get(name)
            action = "update" if prior else "create"
            entry = {"uid": uid, "name": name, "action": action, "panels": len(d["spec"]["panels"])}
            if args.apply:
                body = {"name": name, "spec": d["spec"]}
                if prior:
                    code, resp = c.req("PUT", f"/api/orgs/{org}/dashboards/{prior['id']}", body=body)
                else:
                    code, resp = c.req("POST", f"/api/orgs/{org}/dashboards", body=body)
                entry["status"] = code
                if code in (200, 201):
                    entry["id"] = resp["id"]
                    entry["url"] = f"{c.url}/dashboards/{resp['id']}"
                else:
                    entry["error"] = str(resp)[:300]
                applied["dashboards"][uid] = entry
            else:
                dry["dashboards"].append(entry)
            done(action, f"dashboard {n}/{len(dashboards)}", name, f"({panels(entry['panels'])})", entry)

    if alert_sel:
        code, existing = c.req("GET", f"/api/orgs/{org}/alert-rules")
        if code != 200:
            sys.exit(f"cannot list Cardinal alert rules ({code}): {existing}. Alert rules need a "
                     "Member/Owner of the org: a login token (CARDINAL_TOKEN), cardinal-connect with "
                     "alerts:write, or an org API key with admin:all scope.")
        rules = existing if isinstance(existing, list) else existing.get("rules", existing.get("data", []))
        by_name = {}
        for r in rules:
            spec = r.get("ruleSpec") or r.get("rule_spec") or {}
            by_name[spec.get("name")] = r
        for n, a in alert_sel:
            spec = dict(a["rule_spec"], name=args.name_prefix + a["rule_spec"]["name"])
            prior = by_name.get(spec["name"])
            action = "update" if prior else "create"
            want_enabled = not (a.get("paused") or args.disable_alerts)
            entry = {"name": spec["name"], "action": action, "query": spec["query"]["expr"][:120],
                     "instance": instance.get("slug") or instance["id"], "want_enabled": want_enabled}
            if args.apply:
                body = {"integration_id": instance["id"], "rule_spec": spec}
                if prior:
                    code, resp = c.req("PUT", f"/api/orgs/{org}/alert-rules/{prior['id']}", body=body)
                else:
                    code, resp = c.req("POST", f"/api/orgs/{org}/alert-rules", body=body)
                entry["status"] = code
                if code in (200, 201):
                    entry["id"] = resp.get("id") or (prior or {}).get("id")
                    if not want_enabled or prior:  # an update keeps the old on/off state; set it explicitly
                        problem = set_rule_enabled(c, entry["instance"], entry["id"], want_enabled)
                        if problem:
                            failed += 1
                            entry["note"] = f"{'enable' if want_enabled else 'disable'} FAILED: {problem}"
                    if not entry.get("note"):
                        entry["note"] = ("enabled" if want_enabled else
                                         "disabled: was paused in Grafana" if a.get("paused") else "disabled")
                else:
                    entry["error"] = str(resp)[:300]
                    if code == 422 and "not connected" in str(resp).lower():
                        others = [i.get("slug") for i in lakes.get("all", [lakes["chosen"]]) if i["id"] != instance["id"]]
                        entry["error"] += (f" -- alerting is switched off on data lake '{instance.get('slug')}'. "
                                           f"Other data lakes in this org: {others or 'none'} (--instance <slug>)")
                applied["alerts"][a["name"]] = entry
            else:
                dry["alerts"].append(entry)
            done(action, f"alert rule {n}/{len(alerts)}", spec["name"], f"({entry['note']})" if entry.get("note") else "", entry)
            if entry.get("error") and entry.get("status") in (401, 403, 422):
                break  # would fail the same way for every remaining rule

    if args.apply:
        json.dump(applied, open(os.path.join(args.plan, "applied.json"), "w"), indent=2)
    else:
        json.dump(dry, open(os.path.join(args.plan, "dry-run.json"), "w"), indent=2)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
