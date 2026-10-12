"""Durable development history for the hosted compile/test/accept workflow.

Passing development tests is not independent qualification. The offline engine
owns blind review, bounded repair and the untouched qualification audit.
"""
from contextlib import contextmanager
import fcntl
import json
import re
import uuid
from pathlib import Path
from .engine import gate
from .files import private_json
from .storage import digest


class RegressionHistory:
    def __init__(self, output, scope):
        if not isinstance(scope, str) or not re.fullmatch('[a-f0-9]{64}', scope):
            raise ValueError('invalid compilation scope')
        self.output = Path(output)
        self.root = self.output / 'compilation' / scope
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def locked(self):
        with (self.root / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self):
        path = self.root / 'regression.json'
        return json.loads(path.read_text()) if path.exists() else {
            'cases': {}, 'results': {}, 'incumbent': None, 'accepted': {}}

    def reserve(self, version, service, start, end, trace_ids, expected_verdicts):
        """Invalidate old observations before a test can fail or be interrupted."""
        with self.locked():
            state = self.read()
            token = uuid.uuid4().hex
            created = []
            for trace_id in trace_ids:
                locator = {'service_name': service, 'start': start, 'end': end,
                           'trace_id': trace_id}
                key = digest(locator)
                if key not in state['cases']:
                    created.append(key)
                expected = expected_verdicts.get(trace_id)
                previous = state['cases'].get(key, {})
                if previous.get('expected') and expected and previous['expected'] != expected:
                    raise ValueError('regression gold changed; start explicit requalification with a new contract')
                state['cases'][key] = {'locator': locator, 'expected': previous.get('expected') or expected}
                state['results'].setdefault(version, {}).pop(key, None)
                state.setdefault('pending', {}).setdefault(version, {})[key] = token
            state.setdefault('created', {})[token] = created
            private_json(self.root / 'regression.json', state)
            return token

    def release(self, version, token):
        """Undo a reservation the API rejected before evaluating anything.

        A request rejected as invalid (for example a window over the selection
        limit) is not a development case; cases it created would otherwise be
        impossible to run and block acceptance forever. Cases that existed
        before stay registered and still need a run for this version.
        """
        with self.locked():
            state = self.read()
            pending = state.get('pending', {}).get(version, {})
            for key in [k for k, t in pending.items() if t == token]:
                del pending[key]
            for key in state.get('created', {}).pop(token, []):
                if not any(key in results for results in state['results'].values()):
                    state['cases'].pop(key, None)
            private_json(self.root / 'regression.json', state)

    def record(self, version, service, start, end, result, token, persist_program):
        with self.locked():
            state = self.read()
            pending = state.get('pending', {}).get(version, {})
            for row in result['results']:
                key = digest({'service_name': service, 'start': start, 'end': end,
                              'trace_id': row['trace_id']})
                if pending.get(key) != token:
                    raise ValueError('test result superseded by a newer regression attempt')
                state['results'].setdefault(version, {})[key] = {
                    'receipt': result['test_receipt'], 'trace_id': row['trace_id']}
                del pending[key]
            state.get('created', {}).pop(token, None)
            # Keep the latest teaching receipt and acceptance metadata consistent
            # with the regression ledger while excluding concurrent acceptance.
            persist_program()
            private_json(self.root / 'regression.json', state)

    def check(self, version, state=None):
        state = self.read() if state is None else state
        observations = state['results'].get(version, {})
        missing = sorted(set(state['cases']) - set(observations))
        if not state['cases'] or missing:
            raise ValueError('Run test_behavior on the complete accumulated regression suite before acceptance; '
                             f'{len(missing)} cases missing for this version')
        rows = []
        receipts = {}
        for key, case in sorted(state['cases'].items()):
            observation = observations[key]
            receipt_id = observation['receipt']
            if receipt_id not in receipts:
                receipt = json.loads((self.output / 'teaching' / (receipt_id + '.json')).read_text())
                if digest(receipt) != receipt_id or receipt.get('diagnostic_version') != version:
                    raise ValueError('regression receipt integrity/version mismatch')
                receipts[receipt_id] = receipt
            row = next(r for r in receipts[receipt_id]['results'] if r['trace_id'] == observation['trace_id'])
            if not case['expected'] or row.get('expected_verdict') != case['expected']:
                raise ValueError('Run test_behavior with an expected verdict for every regression trace')
            rows.append({'id': key, 'expected': case['expected'], 'actual': row['verdict'],
                         'mechanical_pass': row.get('mechanical_pass', True) is True})
        result = gate(rows, minimum_per_label=0)
        if not result['passed']:
            raise ValueError('Accumulated regression actual verdicts do not match expectations; incumbent preserved')
        return {'status': 'DEVELOPMENT_PASSED', 'diagnostic_version': version,
                'suite_sha256': digest(state['cases']), 'development_gate': result,
                'test_receipts': sorted(receipts),
                'note': 'Development regression coverage is not independent qualification or held-out correctness.'}

    def accepted(self, version, report, state):
        # Caller holds the lock across check and the API acceptance request.
        state['incumbent'] = version
        state['accepted'][version] = report
        private_json(self.root / 'regression.json', state)

    def begin_replay(self, version):
        with self.locked():
            state = self.read()
            if not state['cases'] or any(not c['expected'] for c in state['cases'].values()):
                raise ValueError('Register expected verdicts for the development suite before replay')
            state['results'][version] = {}
            state.setdefault('pending', {})[version] = {}
            private_json(self.root / 'regression.json', state)
            return list(state['cases'].values())

    def status(self, version):
        with self.locked():
            state = self.read()
            return {'compilation_scope': self.root.name, 'incumbent': state['incumbent'],
                    'diagnostic_version': version, 'cases': list(state['cases'].values()),
                    'tested_cases': len(state['results'].get(version, {})),
                    'required_cases': len(state['cases'])}
