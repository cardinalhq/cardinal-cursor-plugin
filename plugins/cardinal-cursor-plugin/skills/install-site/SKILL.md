---
name: cardinal-install-site
description: Install a Cardinal site (perch + optional POC Cardinal Data Lake) into a Kubernetes cluster you have access to, driven by the Cardinal control-plane API.
---

# Cardinal Install Site

Use this skill when the user asks to install Cardinal, install Cardinal Data Lake, install a Cardinal site, add a Perch operator to a cluster, or set up a Cardinal deployment.

## Prerequisites

- The user ran cardinal-connect first — this skill fails with `no_act_token` otherwise.
- `helm` and `kubectl` are on PATH, and the current kube-context points at the target cluster (`kubectl config current-context`).
- The user is an owner of the Cardinal org they're installing for. The API enforces this — a non-owner token is refused server-side.

## How to run

Drive `python3 scripts/cardinal-install-site <subcommand>` via bash. **Every subcommand prints one JSON object**; parse it and branch on `ok`. On `ok:false`, read `error` + `hint` and tell the user in plain language — never dump raw JSON.

Do the steps in order, pausing for the user where noted. **Do not skip the confirm gate before install-perch**, and **never print the install key** — the script handles it and redacts it; if you ever see a `psk_...` value, something is wrong.

### 1–2. Pick the org

```
python3 scripts/cardinal-install-site whoami
```

Lists `owner_orgs`. If empty, stop: the user isn't an owner of any org and can't install a site (say so; mention `non_owner_count` if > 0). If one, use it. If several, ask which.

### 3. Name the site

```
python3 scripts/cardinal-install-site list-sites --org <orgId>
```

Show existing site names. Ask for a new name. If the name matches an existing site, offer to resume it (skip create, go to step 6/7 against that siteId) or pick another — don't just retry into a `name_taken` error.

### 4–5. Cluster + namespace

- Confirm the target cluster: run `kubectl config current-context` and show it. If it's not the intended cluster, have the user switch context and re-confirm.
- Namespace: suggest `cardinal`. Check it's free with `kubectl get ns cardinal` and re-prompt if it already exists.

### 6. Create the site, then install perch (with a confirm gate)

```
python3 scripts/cardinal-install-site create-site --org <orgId> --name <name> --namespace <ns>
```

Then show the exact command that will run against the cluster:

```
python3 scripts/cardinal-install-site install-perch --org <orgId> --site <siteId> --namespace <ns> --dry-run
```

Print the `command` it returns and **ask the user to confirm** before executing. On approval:

```
python3 scripts/cardinal-install-site install-perch --org <orgId> --site <siteId> --namespace <ns>
```

If this fails (`helm_failed`), the site row still exists and the install key is still recoverable — relay the `stderr`, let the user fix the cluster issue, and re-run the same command. If they want to abandon, `delete-site`.

### 7. Wait for perch to check in

```
python3 scripts/cardinal-install-site wait-perch --org <orgId> --site <siteId>
```

Blocks until perch phones home (up to 5 min). This is a **hard gate** for step 8 — don't proceed on a timeout; help debug the perch pod instead.

### 8. Optionally add a POC Cardinal Data Lake

Ask if they want a POC Cardinal Data Lake (managed object store + Postgres, provisioned in-cluster by perch — no external credentials).

```
python3 scripts/cardinal-install-site add-lakerunner --org <orgId> --site <siteId> --name <name> --namespace <ns>
```

Common stops: `trial_not_eligible` / `contact_sales` (no license — relay it, don't retry); `operator_not_phoned_home` (step 7 didn't actually complete); `all_slots_used` (org has consumed its Cardinal Data Lake slots — relay the hint verbatim). After it returns, run `wait-perch` again so perch reconciles the new workload.

### 9. Verify + hand off

```
python3 scripts/cardinal-install-site verify --org <orgId> --site <siteId>
python3 scripts/cardinal-install-site connect-info --org <orgId> --site <siteId>
```

Show the user the site is up, then print the connect block from `connect-info`: the login email, the `port_forward` command, and the `read_password` command. Say plainly that Maestro never had the password — perch mints it in-cluster, and `read_password` reads it from the Secret. Point them at the site in the Cardinal UI to take it from there.

## Notes

- The token authenticates as `X-CardinalHQ-API-Key`; the script adds `X-Org-Id` itself. Never construct HTTP calls directly — always go through the script.
- If any call returns `unauthorized`, the token was revoked/expired: tell the user to re-run cardinal-connect.
- This skill only ever mutates the cluster once (the approved `helm install`). Every verification is Maestro-side — perch pushes status back — so it works even if this session can't reach the cluster after the install.
