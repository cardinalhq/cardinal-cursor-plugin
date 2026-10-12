#!/usr/bin/env python3
# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""Cardinal behavior MCP: accepted contract, deployed execution, compact findings."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from storyboard import render_storyboard
import checks_ledger
import range_run
from range_runner import RangeRunner
from compilation.files import private_json, private_text
from compilation.hosted import APIRejected, Authoring, SDK_IDENTITIES

ROOT = Path(__file__).resolve().parent
VERSION = '87dde6be3a806f1c9a0346f82d6e861a6ab9bba7aad9d9a70ab884e6427d35f0'
POPULATION = 'cardinal-investigator'
VERDICTS = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR', 'NOT_APPLICABLE')
DESCRIPTION = ('Flag an investigator run when the assistant states it has enough evidence '
               'to close the investigation before its first submit_report invocation. '
               'A statement after submission is not a match. A match does not establish '
               'that its evidence was sufficient or insufficient, or that submission succeeded.')


RUNTIMES = ('claude', 'codex', 'cursor', 'gemini')


def resolve_runtime(runtime=None, environ=None):
    """The agent runtime this server serves: --runtime, else
    CARDINAL_BEHAVIOR_RUNTIME, else claude (Claude Code's .mcp.json passes none)."""
    env = os.environ if environ is None else environ
    value = runtime or env.get('CARDINAL_BEHAVIOR_RUNTIME') or 'claude'
    if value not in RUNTIMES:
        raise ValueError('runtime must be one of ' + ', '.join(RUNTIMES))
    return value


def agent_dir(runtime, home=None):
    """The runtime's agent dir (cardinal_core.paths.AgentPaths home): ~/.<runtime>."""
    return Path(home or Path.home()) / f'.{runtime}'


def _read_object(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def agent_paths_connection(runtime, home=None):
    """(mcp_url, mcp_api_key) from the runtime's managed connect files, as
    cardinal_core.evidence_promote.agent_paths_connection reads them:
    <agent dir>/cardinal.json mcp_url + <agent dir>/cardinal-secrets.json
    mcp_api_key. (None, None) when not connected that way."""
    directory = agent_dir(runtime, home)
    url = _read_object(directory / 'cardinal.json').get('mcp_url')
    key = _read_object(directory / 'cardinal-secrets.json').get('mcp_api_key')
    if not (isinstance(url, str) and url):
        return None, None
    return url, key if isinstance(key, str) and key else None


def behavior_url(mcp_url, label='CARDINAL_MCP_URL'):
    """The org's behavior proxy for a connected organization MCP endpoint."""
    connection = urllib.parse.urlsplit(mcp_url)
    match = re.fullmatch(r'/api/orgs/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/(?:mcp|integrations/[A-Za-z0-9_-]+/mcp)/?', connection.path)
    if (connection.scheme not in ('http', 'https') or not connection.netloc
            or connection.username is not None or connection.password is not None
            or connection.query or connection.fragment or not match):
        raise ValueError(f'{label} must identify the connected organization MCP endpoint')
    return urllib.parse.urlunsplit((connection.scheme, connection.netloc,
                                    f'/api/orgs/{match.group(1)}/behavior', '', ''))


def default_config(environ=None, runtime=None, home=None):
    """Route a normal plugin credential through its existing authenticated host.

    Precedence: CARDINAL_BEHAVIOR_API_URL (+ CARDINAL_QUERY_API_KEY), then
    CARDINAL_MCP_URL (+ CARDINAL_MCP_API_KEY) in the environment (Claude Code
    passes its settings env), then, for codex, cursor and gemini, the
    runtime's own connect files (cardinal.json mcp_url + cardinal-secrets.json
    mcp_api_key). On codex, cursor and gemini an environment URL is taken only
    with its key next to it: a URL alone (a shell that exports
    CARDINAL_MCP_URL for something else) would pair it with no key, or with
    the files' key for another host, so the connect files win. Claude has no
    connect files: its environment URL is taken as it is. The key read from a
    file stays in memory: the range worker reads the same files itself."""
    env = os.environ if environ is None else environ
    runtime = resolve_runtime(runtime, env)
    claude = runtime == 'claude'
    base_url = None
    api_key = None
    api_key_env = 'CARDINAL_MCP_API_KEY'
    if env.get('CARDINAL_BEHAVIOR_API_URL') and (claude or env.get('CARDINAL_QUERY_API_KEY')):
        base_url = env['CARDINAL_BEHAVIOR_API_URL']
        api_key_env = 'CARDINAL_QUERY_API_KEY'
    elif env.get('CARDINAL_MCP_URL') and (claude or env.get('CARDINAL_MCP_API_KEY')):
        base_url = behavior_url(env['CARDINAL_MCP_URL'])
    elif not claude:
        # Claude keeps its key in settings.json env, never in these files.
        url, key = agent_paths_connection(runtime, home)
        try:
            base_url = behavior_url(url) if url else None
        except ValueError:
            # Not an organization MCP endpoint: start anyway; every API tool
            # then says to connect (a malformed file must not kill the server).
            base_url = None
        api_key = key if base_url else None
    config = {'base_url': base_url, 'org': env.get('CARDINAL_ORG_ID'), 'runtime': runtime,
              'api_key_env': api_key_env,
              'output_dir': env.get('CARDINAL_BEHAVIOR_OUTPUT_DIR', str(Path.home() / '.cardinal' / 'behavior-executions'))}
    if api_key:
        config['api_key'] = api_key
    return config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise carries custom API-key headers across redirects.
        return None


def api_error_detail(response, limit=1024):
    """The API's own message for a rejected request: printable text only, never raw bytes."""
    try:
        body = response.read(limit + 1).decode('utf-8', 'replace')
    except OSError:
        return None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            body = str(parsed.get('message') or parsed.get('error') or '')
    except ValueError:
        pass
    text = ' '.join(''.join(c if c.isprintable() else ' ' for c in body).split())
    return text[:limit] or None


def bounded_text(value, limit=1400):
    return str(value)[:limit] if value else None


def compact(raw: dict) -> dict:
    """Allowlist only the proven agent-facing fields; never relay backend objects."""
    gaps = raw.get('coverage_gaps') or (raw.get('validation_observations') or {}).get('coverage_gaps', [])
    reason = raw.get('reason')
    if not reason:
        reason = next((r.get('reason') for r in raw.get('records', [])
                       if r.get('op') == 'violation' and r.get('reason')), None)
    # Preserve the model's compact explanation of actual evidence, without its packet.
    judged = next((r.get('reason') for r in raw.get('jev_receipts', [])
                   if r.get('reason') and (raw['verdict'] != 'MATCH' or r.get('decision') == 'YES' and set(r.get('decision_evidence_refs', [])) & set(raw.get('witness_refs', [])))), None)
    if raw['verdict'] == 'ERROR':
        # An earlier successful decision does not explain a later runtime failure.
        reason = raw.get('error') or next((r.get('reason') for r in raw.get('jev_receipts', [])
                    if r.get('decision') == 'ERROR' and r.get('reason')), None) or raw.get('reason') or 'Trace evaluation failed.'
    elif judged:
        reason = f'{reason} {judged}' if reason else judged
    if not reason and gaps:
        reason = '; '.join(str(g.get('reason', '')) for g in gaps)
    if not reason:
        reason = raw.get('error')
    if not reason and raw['verdict'] == 'NON_MATCH':
        reason = 'No violation recorded; the diagnostic supplied no further reason.'
    return {'trace_id': raw['trace_id'], 'verdict': raw['verdict'],
            'reason': bounded_text(reason),
            'witness_refs': [str(ref)[:256] for ref in raw.get('witness_refs', [])[:32]],
            'coverage_gaps': [{k: bounded_text(g[k], 500) for k in ('ref', 'reason') if k in g}
                              for g in gaps[:16]]}


MCP_TEXT_LIMIT = 1024 * 1024
# A month of one-hour windows; longer ranges are almost certainly a mistake.
MAX_RANGE_WINDOWS = 24 * 31
RANGE_LISTEN = ('The run continues in the background. Listen with Monitor on watch_command (one line per event; it '
                'exits at needs_extents, completed, failed or stalled; re-arm it on expiry). On needs_extents or '
                'stalled call continue_behavior_range; on completed read summary_file (result_file holds every raw '
                'verdict).')
# Runtimes without Claude Code's Monitor poll the MCP status tool instead.
RANGE_POLL = ('The run continues in the background. Poll it with get_behavior_range: range_id, after_event (0 '
              'first, then the previous call\'s next_event) and wait_seconds 20; each call waits up to '
              'wait_seconds for new events and returns one line per event. Stop polling when done is true: on '
              'needs_extents or stalled call continue_behavior_range, then poll again; on failed report the '
              'error; on completed read its summary (result_file holds every raw verdict).')


def range_listen(runtime):
    """How the agent waits for a background range run on this runtime."""
    return RANGE_LISTEN if (runtime or 'claude') == 'claude' else RANGE_POLL


def teaching_verdicts(results) -> list:
    """Per-trace teaching verdicts with bounded evidence; native traces and packets stay on disk."""
    rows = []
    for raw in results:
        records, receipts = raw.get('records', []), raw.get('jev_receipts', [])
        rows.append({**compact(raw), 'expected_verdict': raw.get('expected_verdict'),
                     'records': [{k: bounded_text(record[k], 500) for k in ('op', 'subject', 'reason') if k in record}
                                 for record in records[:32]],
                     'record_count': len(records),
                     'jev_decisions': [{'decision': bounded_text(receipt.get('decision'), 16),
                                        'reason': bounded_text(receipt.get('reason'), 300),
                                        'usage': {k: v for k, v in receipt.get('usage', {}).items()
                                                  if k in ('input_tokens', 'output_tokens') and type(v) is int}}
                                       for receipt in receipts[:8]],
                     'jev_receipt_count': len(receipts)})
    return rows


def mcp_result(name: str, result, output: Path):
    """Bound what crosses stdio: one oversized JSON-RPC line closes the client connection."""
    if name == 'test_behavior' and isinstance(result, dict) and 'results' in result:
        result = {**{k: v for k, v in result.items() if k not in ('results', 'receipt')},
                  'results': teaching_verdicts(result['results']),
                  'detail': 'Native traces, semantic occurrences and JEV evidence packets are retained in receipt_file.'}
    elif name == 'accept_behavior' and isinstance(result, dict) and 'teaching_results' in result:
        result = {**{k: v for k, v in result.items() if k != 'teaching_results'},
                  'teaching_results': [{k: item.get(k) for k in ('trace_id', 'expected_verdict', 'verdict')}
                                       for item in result['teaching_results']]}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    size = len(text.encode('utf-8'))
    if size <= MCP_TEXT_LIMIT:
        return text
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    path = output / 'mcp-results' / f'{name}-{digest}.json'
    private_text(path, text)
    small = {k: v for k, v in result.items() if len(json.dumps(v, ensure_ascii=False)) <= 4096} \
        if isinstance(result, dict) else {}
    return json.dumps({'result_file': str(path), 'result_bytes': size, 'result_sha256': digest,
                       'omitted_fields': sorted(set(result) - set(small)) if isinstance(result, dict) else [],
                       **small}, ensure_ascii=False, indent=2)


class Behavior(Authoring):
    def __init__(self, config: dict):
        self.config = config
        self.output = Path(config['output_dir']).expanduser().resolve()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sdk_observation = None
        self.legacy = bool(config.get('diagnostic_version') or config.get('artifacts'))
        self.version = config.get('diagnostic_version', VERSION) if self.legacy else None
        if not self.legacy:
            self.population = self.description = None
            return
        self.population = config.get('population', POPULATION)
        self.description = config.get('description', DESCRIPTION)
        if not isinstance(self.version, str) or not re.fullmatch('[a-f0-9]{64}', self.version):
            raise ValueError('diagnostic_version must be a SHA-256 content address')
        if self.version != VERSION and any(not config.get(k) for k in ('population', 'description', 'artifacts')):
            raise ValueError('a custom DiagnosticVersion requires population, description, and artifacts')
        if not isinstance(self.population, str) or not self.population.strip():
            raise ValueError('population must be a nonempty service name')
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError('description must be nonempty')
        artifacts = Path(config.get('artifacts', ROOT / 'artifacts')).expanduser()
        self.definition = json.loads((artifacts / 'versions' / f'{self.version}.json').read_text())
        payload = {k: v for k, v in self.definition.items() if k != 'version'}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if self.definition['version'] != self.version or digest != self.version:
            raise ValueError('accepted DiagnosticVersion integrity check failed')
        self.adapter_sha256 = config.get('adapter_sha256', self.definition['profile_sha256'])
        if not isinstance(self.adapter_sha256, str) or not re.fullmatch('[a-f0-9]{64}', self.adapter_sha256):
            raise ValueError('adapter_sha256 must be a SHA-256 content address')
        self.preview = json.loads((artifacts / 'previews' / f'{self.definition["preview_version"]}.json').read_text())
        preview_payload = {k: v for k, v in self.preview.items() if k != 'version'}
        preview_digest = hashlib.sha256(json.dumps(preview_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if preview_digest != self.definition['preview_version']:
            raise ValueError('accepted contract preview integrity check failed')

    def request(self, method: str, path: str, payload=None) -> dict:
        if not self.config.get('base_url'):
            raise ValueError('Connect the Cardinal plugin, or configure CARDINAL_BEHAVIOR_API_URL with a matching data-plane credential')
        headers = {'Content-Type': 'application/json'}
        if self.config.get('headers_file'):
            headers_path = Path(self.config['headers_file']).expanduser()
            if headers_path.stat().st_mode & 0o077:
                raise ValueError('headers_file must be private (mode 0600)')
            configured = json.loads(headers_path.read_text())
            allowed = {'x-chq-internal-key', 'x-chq-internal-org-id', 'x-cardinalhq-api-key'}
            if set(k.lower() for k in configured) - allowed:
                raise ValueError('unsupported authentication header')
            headers.update(configured)
        elif self.config.get('internal_key_env'):
            headers['x-chq-internal-key'] = os.environ[self.config['internal_key_env']]
            headers['x-chq-internal-org-id'] = self.config['org']
        else:
            key_env = self.config.get('api_key_env', 'CARDINAL_MCP_API_KEY')
            key = self.config.get('api_key') or os.environ.get(key_env)
            if not key:
                raise ValueError(f'{key_env} must contain the configured API credential')
            headers['x-cardinalhq-api-key'] = key
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.config['base_url'].rstrip('/') + path,
                                         data=data, headers=headers, method=method)
        # Compiler/acceptance teaching checks run under the API's five-minute bound.
        timeout = 310 if method == 'POST' and (path in ('/api/v1/behavior-programs/compile', '/api/v1/behavior-programs/test') or path.endswith('/accept')) else 60
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            detail = api_error_detail(exc) if 400 <= status < 500 and status not in (401, 403) else None
            exc.close()
            if detail:
                # Request and validation errors explain how to fix the call; relay them bounded.
                raise APIRejected(f'Cardinal API rejected the request (HTTP {status}): {detail}') from None
            raise RuntimeError(f'Cardinal API returned HTTP {status}; check the configured service and credentials') from None
        except urllib.error.URLError:
            raise RuntimeError('Cardinal API is unreachable; check the configured service') from None
        except (OSError, http.client.HTTPException):
            # e.g. RemoteDisconnected: a gateway closed a slow request without a response.
            raise RuntimeError('Cardinal API closed the connection or timed out before answering') from None

    def state_path(self, execution_id):
        if not re.fullmatch('[a-f0-9]{32}', execution_id):
            raise ValueError('invalid execution reference')
        return self.output / execution_id / 'receipt.json'

    def read(self, execution_id):
        state = json.loads(self.state_path(execution_id).read_text())
        if state['execution_id'] != execution_id or (self.legacy and state['diagnostic_version'] != self.version):
            raise ValueError('receipt identity mismatch')
        return state

    def select(self):
        selection = {'diagnostic_version': self.version, 'accepted_behavior': self.version,
                'description': self.description, 'population': self.population,
                'contract': [c['interpretation'] for c in self.preview['clauses']],
                'window': {'start': self.config['start'], 'end': self.config['end']},
                'selection': 'Existing accepted DiagnosticVersion; no compilation performed by this tool.'}
        if self.config.get('compile_receipt_ref'):
            selection['compile_receipt_ref'] = str(self.config['compile_receipt_ref'])
        return selection

    def context(self, version):
        if self.legacy:
            if version != self.version:
                raise ValueError('unsupported accepted behavior')
            return self.definition, self.preview, self.adapter_sha256
        path = self.program_path(version)
        if not path.exists():
            raise ValueError('Inspect and accept this version before executing it')
        program = json.loads(path.read_text())
        definition = {'version': version, 'diagnostic_id': program.get('diagnostic_id', version),
                      'profile_sha256': program['profile_sha256'], 'source_sha256': program['source_sha256'],
                      **self.identities(program), 'sdk_source': self.sdk_source(program)}
        return definition, program['behavior_contract'], program['adapter_sha256']

    def start(self, accepted_behavior, population, start=None, end=None):
        self.context(accepted_behavior)
        if self.legacy and population != self.population:
            raise ValueError('unsupported accepted behavior or population')
        if not self.legacy:
            program = json.loads(self.program_path(accepted_behavior).read_text())
            if not program.get('acceptance_id'):
                raise ValueError('Accept this immutable DiagnosticVersion before executing it')
        start = start or self.config.get('start')
        end = end or self.config.get('end')
        if not start or not end:
            raise ValueError('start and end are required for the execution population')
        payload = {'diagnostic_version': accepted_behavior,
                   'service_name': population, 'start': start, 'end': end}
        if self.config.get('org'):
            payload['org'] = self.config['org']
        response = self.request('POST', '/api/v1/behavior-executions', payload)
        execution_id = response['execution_id']
        state = {'execution_id': execution_id, 'diagnostic_version': accepted_behavior,
                 'population_specification': payload, 'status': response['status'],
                 'cursor': 0, 'results': [], 'counts': {}, 'receipt': None,
                 'submission': {'method': 'POST', 'url': self.config['base_url'].rstrip('/') + '/api/v1/behavior-executions',
                                'submitted_at': time.time(), 'response': response}}
        private_json(self.state_path(execution_id), state)
        return {'execution_id': execution_id, 'execution_status': state['status'],
                'diagnostic_version': accepted_behavior, 'population': population,
                'submission': {'method': 'POST', 'endpoint': state['submission']['url'], 'accepted': True}}

    def poll(self, execution_id, wait_seconds=20):
        state = self.read(execution_id)
        definition, _, adapter_sha256 = self.context(state['diagnostic_version'])
        deadline = time.monotonic() + max(0, min(30, float(wait_seconds)))
        fresh = []
        while True:
            page = self.request('GET', f'/api/v1/behavior-executions/{execution_id}/results?after_result_seq={state["cursor"]}')
            if page['execution_id'] != execution_id:
                raise ValueError('API returned a different execution')
            if page['next_cursor'] < state['cursor']:
                raise ValueError('API cursor moved backwards')
            seen = {r['trace_id'] for r in state['results']}
            for item in page['results']:
                if item['trace_id'] in seen:
                    raise ValueError('API returned a duplicate trace result')
                if item['verdict'] not in VERDICTS:
                    raise ValueError('API returned an invalid verdict')
                identity = item.get('execution_identity', {})
                expected = {'diagnostic_version': state['diagnostic_version'], 'profile_sha256': definition['profile_sha256'],
                            'udf_sha256': definition['source_sha256'],
                            'adapter_sha256': adapter_sha256}
                if not self.legacy:
                    expected.update(self.identities(definition), sdk_source=definition['sdk_source'])
                if any(identity.get(k) != v for k, v in expected.items()):
                    raise ValueError('result does not belong to the accepted DiagnosticVersion')
                seen.add(item['trace_id'])
                fresh.append(item)
                state['results'].append(item)
            state.update(cursor=page['next_cursor'], status=page['status'], counts=page['counts'])
            # Raw evidence and backend metadata stay in this private host-side file.
            state['last_response'] = page
            state['receipt'] = None
            complete = page['status'] == 'COMPLETED' and len(state['results']) == page['counts']['population']
            if complete:
                if page.get('receipt') != execution_id:
                    raise ValueError('completed receipt identity mismatch')
                actual = Counter(r['verdict'] for r in state['results'])
                # Deployments before NOT_APPLICABLE was a deployed verdict omit its count.
                if any(actual[v] != page['counts'].get(v, 0) for v in VERDICTS):
                    raise ValueError('completed receipt counts do not match results')
                state['receipt'] = execution_id
            private_json(self.state_path(execution_id), state)
            if fresh or complete or page['status'] == 'FAILED' or time.monotonic() >= deadline:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        response = {'execution_id': execution_id, 'execution_status': state['status'],
                    'results': [compact(r) for r in fresh], 'received': len(state['results']),
                    'population_size': state['counts']['population'] if state['status'] == 'COMPLETED' else None,
                    'evaluated_so_far': state['counts']['population']}
        if state['receipt']:
            response.update(receipt=execution_id, verdicts={v: state['counts'].get(v, 0) for v in VERDICTS})
        elif state['status'] == 'FAILED':
            response['error'] = 'Deployed execution failed; retained evidence is available for operator review.'
        else:
            response['next_action'] = 'Call next_behavior_result again until a receipt appears.'
        return response

    def hydrate(self, state, trace_id):
        prior = next((item for item in state['results'] if item['trace_id'] == trace_id), None)
        if prior is None:
            raise ValueError('trace is not present in this execution receipt')
        if self.legacy:
            return prior
        query = urllib.parse.urlencode({'trace_id': trace_id})
        result = self.request('GET', f'/api/v1/behavior-executions/{state["execution_id"]}/receipt?{query}')
        if any(result.get(key) != prior.get(key) for key in ('trace_id', 'verdict', 'execution_identity')):
            raise ValueError('durable trace receipt identity mismatch')
        state['results'][state['results'].index(prior)] = result
        private_json(self.state_path(state['execution_id']), state)
        return result

    def render(self, receipt, accepted_behavior):
        state = self.read(receipt)
        if accepted_behavior != state['diagnostic_version']:
            raise ValueError('accepted behavior does not match this execution')
        definition, preview, _ = self.context(accepted_behavior)
        if state['status'] != 'COMPLETED' or state.get('receipt') != receipt:
            raise ValueError('Storyboard requires a completed execution receipt')
        for trace_id in [item['trace_id'] for item in state['results']]:
            self.hydrate(state, trace_id)
        output = self.state_path(receipt).parent / f'storyboard-{receipt}.html'
        return render_storyboard(state, definition, preview, output)

    def inspect(self, execution_id, trace_id=None, after_jev=0):
        """Inspect observed receipt metadata without returning model input packets."""
        if type(after_jev) is not int or after_jev < 0:
            raise ValueError('after_jev must be a nonnegative integer')
        state = self.read(execution_id)
        response = {'execution_id': execution_id, 'diagnostic_version': state['diagnostic_version'],
                    'execution_status': state['status'], 'receipt': state.get('receipt'),
                    'received': len(state['results']), 'counts': state['counts'],
                    'receipt_file': str(self.state_path(execution_id)),
                    'observation': 'Retained through next_behavior_result; poll for newer commits.',
                    'shards': [{k: shard[k] for k in ('shard', 'status', 'root_ref', 'root_sha256', 'generation')
                                if k in shard}
                               for shard in state.get('last_response', {}).get('shards', [])[:4]]}
        if trace_id is not None:
            result = self.hydrate(state, trace_id)
            response['finding'] = compact(result)
            receipts = result.get('jev_receipts', [])
            summaries = []
            for receipt in receipts[after_jev:after_jev + 8]:
                summaries.append({
                    'receipt_id': bounded_text(receipt.get('receipt_id'), 64),
                    'proposition': bounded_text(receipt.get('proposition'), 1000),
                    'evidence_refs': [bounded_text(item.get('ref'), 256) for item in receipt.get('evidence', [])[:64]],
                    'packet_sha256': bounded_text(receipt.get('packet_sha256'), 64),
                    'request_sha256': bounded_text(receipt.get('request_sha256'), 64),
                    'config_digest': bounded_text(receipt.get('config_digest'), 64),
                    'model': bounded_text(receipt.get('config', {}).get('model'), 256),
                    'model_version': bounded_text(receipt.get('config', {}).get('model_version'), 256),
                    'decision': bounded_text(receipt.get('decision'), 16),
                    'parse_status': bounded_text(receipt.get('parse_status'), 64),
                    'reason': bounded_text(receipt.get('reason'), 300),
                    'usage': {k: v for k, v in receipt.get('usage', {}).items()
                              if k in ('input_tokens', 'output_tokens') and type(v) is int},
                    'cost_usd': receipt.get('cost_usd') if type(receipt.get('cost_usd')) in (int, float) else None,
                    'replay_key': bounded_text(receipt.get('replay_key'), 64),
                    'attempts': [{'status': bounded_text(a.get('status'), 64),
                                  'error': bounded_text(a.get('error'), 256)}
                                 for a in receipt.get('attempts', [])[:3]]})
            response.update(jev_receipts=summaries, jev_receipt_count=len(receipts),
                            next_jev=after_jev + len(summaries) if after_jev + len(summaries) < len(receipts) else None)
        return response

    # Range execution runs in a detached worker (range_runner.py) that writes
    # events and results to local files; these tools only start or continue it.

    def start_range(self, accepted_behavior, population, start, end):
        if self.legacy:
            raise ValueError('Range execution needs an accepted authored check')
        self.context(accepted_behavior)
        program = json.loads(self.program_path(accepted_behavior).read_text())
        if not program.get('acceptance_id'):
            raise ValueError('Accept this immutable DiagnosticVersion before executing it')
        judge_based = (any((c.get('judge_site') or '').strip()
                           for c in (program.get('behavior_contract') or {}).get('clauses', []))
                       or range_run.uses_judge(program.get('source')))
        runner = RangeRunner.create(self, accepted_behavior, population, start, end, judge_based, MAX_RANGE_WINDOWS)
        spawned = runner.spawn(self.config.get('_config_path'))
        # The new worker may not hold its lock yet.
        return {**runner.snapshot(), 'worker_running': True if spawned else runner.worker_running(),
                'next_action': range_listen(self.config.get('runtime'))}

    def continue_range(self, range_id, trace_extents=None):
        runner = RangeRunner(self, range_id)
        if trace_extents is not None:
            runner.supply_extents(trace_extents)
        state = runner.read_state()
        restarted = False
        if state['phase'] not in ('completed', 'failed', 'edges') and not runner.worker_running():
            # Written before the worker starts, so a watch started now begins here.
            runner.emit('resumed', phase=state['phase'])
            restarted = runner.spawn(self.config.get('_config_path'))
        snapshot = runner.snapshot()
        if restarted:
            snapshot['worker_running'] = True
        return {**snapshot, 'worker_restarted': bool(restarted), 'next_action': range_listen(self.config.get('runtime'))}


class Checks:
    """The Checks preview's ledger and reads behind MCP tools (checks_ledger):
    the teaching-label ledger under ~/.<runtime>/cardinal/checks/ and the
    accepted checks and range runs under the behavior output directory. The
    stdio server runs outside the agent's sandbox, so these writes work on
    every runtime."""

    def __init__(self, behavior):
        self.output = behavior.output
        self.runtime = behavior.config.get('runtime') or 'claude'

    @property
    def state(self):
        return checks_ledger.state_dir(self.runtime)

    def set_example(self, draft, trace_id, evidence, proposed, reason, missing=None):
        if evidence not in ('sufficient', 'insufficient'):
            raise ValueError('evidence must be sufficient or insufficient')
        return checks_ledger.set_example(draft, trace_id, evidence == 'sufficient', proposed, reason, missing,
                                         state=self.state)

    def approve(self, draft, trace_id, label=None, user_reply=None):
        return checks_ledger.approve(draft, trace_id, label, state=self.state, approved_via='mcp',
                                     user_reply=user_reply)

    def remove(self, draft, trace_id):
        return {'removed': checks_ledger.remove_example(draft, trace_id, state=self.state)}

    def show(self, draft):
        return checks_ledger.read_draft(draft, self.state)

    def lock(self, draft):
        return {'draft': draft, 'expected_verdicts': checks_ledger.expected_verdicts(draft, state=self.state),
                'locked': True}

    def accepted(self, service_name=None):
        return {'checks': checks_ledger.accepted_checks(self.output, service_name)}

    def range(self, range_id, after_event=0, wait_seconds=20):
        return checks_ledger.range_poll(range_id, after_event, wait_seconds, output=self.output)


def tools_list(behavior: Behavior):
    def tool(name, description, properties, required):
        return {'name': name, 'description': description,
                'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
    version = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    if behavior.legacy:
        version['enum'] = [behavior.version]
    population = {'type': 'string', 'minLength': 1}
    if behavior.legacy:
        population['enum'] = [behavior.population]
    execution_properties = {'accepted_behavior': version, 'population': population,
                            'start': {'type': 'string', 'format': 'date-time'},
                            'end': {'type': 'string', 'format': 'date-time'}}
    execution = {'type': 'string', 'description': 'Reference returned by execute_behavior'}
    range_id = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
    draft = {'type': 'string', 'pattern': '^[A-Za-z0-9_-]{1,64}$', 'description': 'The check being built, e.g. <service>-<short-name>-v1'}
    trace = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
    label = {'type': 'string', 'enum': list(checks_ledger.VERDICTS)}
    authoring = [
        tool('get_behavior_sdk', 'Read the exact deployed SDK contract, example UDF and CompilePlan schema before authoring a candidate.', {}, []),
        tool('compile_behavior', 'Compile the authored candidate against the exact SDK read with get_behavior_sdk. Performs syntax/import/API/mechanical validation. Then run test_behavior on real teaching traces, inspect evidence, revise if needed, and explicitly accept. Optional authored examples are compiler checks only.',
             {'description': {'type': 'string', 'minLength': 1}, 'service_name': population,
              'udf_source': {'type': 'string', 'minLength': 1}, 'compile_plan': {'type': 'object'},
              'teaching_examples': {'type': 'array', 'minItems': 0, 'maxItems': 8, 'items': {'type': 'object', 'properties': {'trace': {'type': 'object'}, 'expected_verdict': {'type': 'string', 'enum': ['MATCH', 'NON_MATCH', 'UNKNOWN', 'NOT_APPLICABLE']}}, 'required': ['trace'], 'additionalProperties': False},
                                    'description': 'Authored normalized traces with trace_id and optional expected_verdict; see get_behavior_sdk.'}},
             ['description', 'service_name', 'udf_source', 'compile_plan']),
        tool('test_behavior', 'Run a compiled candidate on 1 to 8 real teaching traces using the deployed production runtime and JEV before acceptance. Returns MATCH/NON_MATCH/UNKNOWN/ERROR, witnesses, Recorder output, JEV receipts and a retained teaching receipt. Supply expected_verdicts for every trace before acceptance; compile revised source before retesting.',
             {'diagnostic_version': version, 'trace_ids': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'uniqueItems': True, 'items': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}},
              'service_name': population, 'start': {'type': 'string', 'format': 'date-time'},
              'end': {'type': 'string', 'format': 'date-time'},
              'udf_source': {'type': 'string', 'description': 'Optional source must exactly match the compiled version.'},
              'expected_verdicts': {'type': 'object', 'additionalProperties': {'type': 'string', 'enum': ['MATCH', 'NON_MATCH', 'UNKNOWN', 'NOT_APPLICABLE']}}},
             ['diagnostic_version', 'trace_ids', 'service_name', 'start', 'end']),
        tool('inspect_behavior', 'Read an immutable compiled Behavior Contract, Python source and compile receipt before acceptance.',
             {'diagnostic_version': version}, ['diagnostic_version']),
        tool('get_behavior_compilation', 'Inspect the accumulated development regression suite, tested coverage and preserved incumbent for a compiled candidate. Compilation does not promote it.',
             {'diagnostic_version': version}, ['diagnostic_version']),
        tool('run_behavior_regressions', 'Execute a repaired candidate on the complete accumulated real-trace development suite, including earlier failed batches. ERROR, missing cases and verdict regressions block acceptance and preserve the incumbent. This is development testing, not independent qualification.',
             {'diagnostic_version': version}, ['diagnostic_version']),
        tool('accept_behavior', 'Accept the inspected immutable DiagnosticVersion for deployed execution. Call only after reviewing its contract and source against the user request.',
             {'diagnostic_version': version}, ['diagnostic_version'])]
    if behavior.legacy:
        authoring = [tool('select_behavior', 'Inspect the configured accepted behavior. Read its contract and select it only if it matches the user request. ' + behavior.description, {}, [])]
    return {'tools': authoring + [
        tool('execute_behavior', 'Submit an explicitly accepted DiagnosticVersion against a service population and a window of at most one hour (maximum 256 selected objects / 256 MiB). Narrow the window if the API rejects its size. Deployed execution runs independently of polling.',
             execution_properties, ['accepted_behavior', 'population'] + ([] if behavior.legacy else ['start', 'end'])),
        tool('next_behavior_result', 'Observe newly committed compact findings while Cardinal investigates. Repeat until receipt appears. Empty results mean the execution is still running; do not fabricate findings. The total population is unknown until COMPLETED: evaluated_so_far is progress, never a total denominator.',
             {'execution_id': execution, 'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 30, 'default': 20}}, ['execution_id']),
        tool('get_behavior_execution', 'Inspect the retained execution receipt and durable shard references. Optionally inspect bounded JEV receipt metadata for one trace; model input text stays in the receipt artifact. This does not advance result polling.',
             {'execution_id': execution, 'trace_id': {'type': 'string'},
              'after_jev': {'type': 'integer', 'minimum': 0, 'default': 0}}, ['execution_id']),
        tool('render_storyboard', 'Render the existing evidence Storyboard from a completed execution receipt. Evidence remains expandable in the HTML; raw receipts never enter this chat.',
             {'receipt': execution, 'accepted_behavior': version}, ['receipt', 'accepted_behavior'])]
        + ([] if behavior.legacy else [
        tool('execute_behavior_range', 'Start running an accepted DiagnosticVersion over any time range in the background: consecutive windows of at most one hour, one at a time, through execute_behavior; a window rejected for size, or whose results cannot be read, is split and rerun. Returns at once with range_id, the events file and a way to follow it (watch_command or poll: next_action says which); results land in local files (summary_file, result_file). Verdicts, judge use and receipts are unchanged.',
             execution_properties, ['accepted_behavior', 'population', 'start', 'end']),
        tool('continue_behavior_range', 'Continue a background range run without waiting: supply trace_extents after a needs_extents event (edge traces are then re-evaluated whole where they fit within an hour; others become UNKNOWN window-edge), or restart a stalled worker from its saved state. Returns a snapshot read from local files.',
             {'range_id': range_id,
              'trace_extents': {'type': 'object', 'description': '{trace_id: {start, end}}: earliest span start and latest span end (RFC3339) per trace, from the query tools.',
                                'additionalProperties': {'type': 'object', 'properties': {'start': {'type': 'string'}, 'end': {'type': 'string'}},
                                                         'required': ['start', 'end'], 'additionalProperties': False}}},
             ['range_id']),
        tool('get_behavior_range', 'Read a background range run: the events after after_event as one line each, at most 50 per call (0 = the current segment, its latest 50 when longer, with counts and how to page; then pass the previous next_event), waiting up to wait_seconds for one. done is true at needs_extents, completed, failed or stalled; next_action says what to do; a completed run includes its summary. Read-only.',
             {'range_id': range_id, 'after_event': {'type': 'integer', 'minimum': 0, 'default': 0},
              'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 30, 'default': 20}}, ['range_id']),
        tool('list_accepted_checks', 'Checks accepted earlier (newest first): contract description, service, accepted diagnostic_version. Reuse one whose contract matches the request.',
             {'service_name': {'type': 'string', 'minLength': 1}}, []),
        tool('set_check_example', 'Record a teaching example in the label ledger with its mechanical evidence-sufficiency result and your proposed label under the approved contract. Insufficient evidence is UNKNOWN by construction and must name what is missing. At most 8 examples per draft.',
             {'draft': draft, 'trace_id': trace, 'evidence': {'type': 'string', 'enum': ['sufficient', 'insufficient']},
              'proposed': label, 'reason': {'type': 'string', 'minLength': 1},
              'missing': {'type': 'string', 'description': 'The evidence that is not there (insufficient only).'}},
             ['draft', 'trace_id', 'evidence', 'proposed', 'reason']),
        tool('approve_check_example', "Record the user's approval of a proposed label, or their correction (label), only after the user gave it. Pass user_reply: the user's reply, quoted verbatim. The ledger records how (mcp) and when it was approved. Refused, with the reason to tell the user, when the correction contradicts insufficient evidence or the labels are locked.",
             {'draft': draft, 'trace_id': trace, 'label': label,
              'user_reply': {'type': 'string', 'maxLength': checks_ledger.MAX_USER_REPLY,
                             'description': "The user's approving or correcting reply, quoted verbatim."}},
             ['draft', 'trace_id']),
        tool('remove_check_example', 'Drop a candidate example (a mistyped trace id, a swapped-out example) until the labels are locked.',
             {'draft': draft, 'trace_id': trace}, ['draft', 'trace_id']),
        tool('get_check_draft', 'Show a draft\'s recorded examples, their labels and whether they are locked.',
             {'draft': draft}, ['draft']),
        tool('lock_check_examples', 'Return expected_verdicts for test_behavior and LOCK the labels: every example must be approved. From now on they are the regression suite\'s gold; a changed label needs a revised contract (a new draft).',
             {'draft': draft}, ['draft'])])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('CARDINAL_BEHAVIOR_CONFIG'))
    parser.add_argument('--runtime', choices=RUNTIMES,
                        help='the agent runtime this server serves (default: CARDINAL_BEHAVIOR_RUNTIME, else claude); '
                             'selects its connection files, its checks ledger and how range runs are waited for')
    args = parser.parse_args()
    runtime = resolve_runtime(args.runtime)
    config = json.loads(Path(args.config).expanduser().read_text()) if args.config else default_config(runtime=runtime)
    if args.config:
        # The background range worker loads the same configuration.
        config['_config_path'] = str(Path(args.config).expanduser().resolve())
        config.setdefault('runtime', runtime)
    behavior = Behavior(config)
    checks = Checks(behavior)
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        try:
            if method == 'initialize':
                value = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                         'serverInfo': {'name': 'cardinal-behavior', 'version': '0.3.0'}}
            elif method == 'tools/list':
                value = tools_list(behavior)
            elif method == 'ping':
                value = {}
            elif method == 'tools/call':
                params = request['params']
                functions = {'get_behavior_sdk': behavior.sdk, 'compile_behavior': behavior.compile, 'inspect_behavior': behavior.inspect_program,
                             'test_behavior': behavior.test, 'accept_behavior': behavior.accept,
                             'get_behavior_compilation': behavior.compilation_status,
                             'run_behavior_regressions': behavior.run_regressions,
                             'select_behavior': behavior.select, 'execute_behavior': behavior.start,
                             'next_behavior_result': behavior.poll, 'render_storyboard': behavior.render,
                             'get_behavior_execution': behavior.inspect,
                             'execute_behavior_range': behavior.start_range,
                             'continue_behavior_range': behavior.continue_range,
                             'get_behavior_range': checks.range, 'list_accepted_checks': checks.accepted,
                             'set_check_example': checks.set_example, 'approve_check_example': checks.approve,
                             'remove_check_example': checks.remove, 'get_check_draft': checks.show,
                             'lock_check_examples': checks.lock}
                result = functions[params['name']](**params.get('arguments', {}))
                value = {'content': [{'type': 'text', 'text': mcp_result(params['name'], result, behavior.output)}]}
            else:
                raise ValueError('unsupported MCP method')
        except Exception as exc:
            # Do not echo exception payloads from network, credentials, or raw result parsing.
            safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            value = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps({'error': safe})}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': value}), flush=True)


if __name__ == '__main__':
    main()
