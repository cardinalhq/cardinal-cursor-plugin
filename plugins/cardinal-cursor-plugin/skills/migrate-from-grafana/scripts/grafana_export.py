#!/usr/bin/env python3
"""Export dashboards, alert rules and datasources from a Grafana instance.

Reads GRAFANA_URL and GRAFANA_TOKEN from the environment (or --env-file).
A Viewer-role service account token is enough: every call here is a read.

Usage:
  grafana_export.py --out ./export [--folder "Griffin Demo"] [--tag prod] [--uid abc --uid def]
                    [--no-alerts] [--env-file .env.grafana-migrate]

Writes:
  export/datasources.json          uid -> {type, name}
  export/dashboards/<uid>.json     full dashboard model (the "dashboard" object)
  export/alerts.json               Grafana-managed rule groups: [{folder, group, interval, rules:[...]}]
  export/summary.json              counts + what was filtered
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def load_env_file(path):
    if not path:
        return
    for line in open(path):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


class Grafana:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def get(self, path, params=None):
        if params:
            path += ("&" if "?" in path else "?") + urllib.parse.urlencode(params, doseq=True)
        req = urllib.request.Request(self.url + path, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:500]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--env-file")
    ap.add_argument("--folder", action="append", default=[], help="folder title or uid (repeatable)")
    ap.add_argument("--tag", action="append", default=[], help="only dashboards with this tag (repeatable)")
    ap.add_argument("--uid", action="append", default=[], help="specific dashboard uid (repeatable)")
    ap.add_argument("--no-alerts", action="store_true")
    args = ap.parse_args()

    load_env_file(args.env_file)
    url, token = os.environ.get("GRAFANA_URL"), os.environ.get("GRAFANA_TOKEN")
    if not url or not token:
        sys.exit("GRAFANA_URL and GRAFANA_TOKEN must be set (env or --env-file)")
    g = Grafana(url, token)

    code, health = g.get("/api/health")
    code_user, _ = g.get("/api/search", {"limit": 1})
    if code_user != 200:
        sys.exit(f"Grafana token rejected ({code_user}). Check GRAFANA_URL / GRAFANA_TOKEN.")

    os.makedirs(os.path.join(args.out, "dashboards"), exist_ok=True)

    # Datasources: needed to tell prometheus/loki/tempo targets apart when a
    # panel references a datasource only by uid.
    code, dss = g.get("/api/datasources")
    datasources = {}
    if code == 200:
        for d in dss:
            datasources[d["uid"]] = {"type": d["type"], "name": d["name"]}
    else:
        print(f"warning: could not list datasources ({code}); datasource types will be inferred from queries", file=sys.stderr)
    json.dump(datasources, open(os.path.join(args.out, "datasources.json"), "w"), indent=2)

    # Resolve folder filters (title or uid) to uids.
    folder_uids = set()
    if args.folder:
        code, folders = g.get("/api/folders", {"limit": 1000})
        folders = folders if code == 200 else []
        for f in args.folder:
            match = [x["uid"] for x in folders if x["uid"] == f or x["title"].lower() == f.lower()]
            if not match:
                # Folders the token can't list may still be searchable by uid.
                match = [f]
            folder_uids.update(match)

    params = {"type": "dash-db", "limit": 5000}
    if args.tag:
        params["tag"] = args.tag
    if folder_uids:
        params["folderUIDs"] = sorted(folder_uids)
    code, hits = g.get("/api/search", params)
    if code != 200:
        sys.exit(f"dashboard search failed: {code} {hits}")
    if args.uid:
        hits = [h for h in hits if h["uid"] in args.uid] or [{"uid": u} for u in args.uid]

    exported = []
    for h in hits:
        code, body = g.get(f"/api/dashboards/uid/{h['uid']}")
        if code != 200:
            print(f"warning: skipping dashboard {h['uid']}: {code} {body}", file=sys.stderr)
            continue
        dash = body["dashboard"]
        dash["_folder"] = body.get("meta", {}).get("folderTitle")
        json.dump(dash, open(os.path.join(args.out, "dashboards", f"{dash['uid']}.json"), "w"), indent=2)
        exported.append({"uid": dash["uid"], "title": dash.get("title"), "folder": dash["_folder"],
                         "panels": sum(1 + len(p.get("panels", [])) for p in dash.get("panels", []))})

    groups = []
    alert_note = None
    if not args.no_alerts:
        code, body = g.get("/api/ruler/grafana/api/v1/rules")
        if code == 200 and isinstance(body, dict):
            # body: {folderTitleOrUid: [ {name, interval, rules:[...]}, ... ]}
            wanted = {f.lower() for f in args.folder} | {u.lower() for u in folder_uids}
            for folder, gs in body.items():
                if wanted and folder.lower() not in wanted:
                    continue
                for grp in gs:
                    groups.append({"folder": folder, "group": grp.get("name"),
                                   "interval": grp.get("interval", "1m"), "rules": grp.get("rules", [])})
        else:
            alert_note = f"alert rules not exported ({code}): {str(body)[:200]}"
            print("warning: " + alert_note, file=sys.stderr)
    json.dump(groups, open(os.path.join(args.out, "alerts.json"), "w"), indent=2)

    summary = {
        "grafana_url": g.url,
        "grafana_version": (health or {}).get("version") if isinstance(health, dict) else None,
        "filters": {"folder": args.folder, "tag": args.tag, "uid": args.uid},
        "dashboards": exported,
        "alert_groups": len(groups),
        "alert_rules": sum(len(x["rules"]) for x in groups),
        "alert_note": alert_note,
        "datasource_types": sorted({d["type"] for d in datasources.values()}),
    }
    json.dump(summary, open(os.path.join(args.out, "summary.json"), "w"), indent=2)
    print(json.dumps({k: summary[k] for k in ("dashboards", "alert_rules", "alert_note")}, indent=2))


if __name__ == "__main__":
    main()
