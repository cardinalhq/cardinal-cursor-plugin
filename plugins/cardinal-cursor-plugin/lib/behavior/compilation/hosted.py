"""Plugin-owned authoring bridge to the existing deployed SDK compiler/runtime."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from .files import private_json, private_text, program_lock
from .storage import digest
from .regression import RegressionHistory

SDK_IDENTITIES = ('sdk_runtime_sha256', 'profile_sha256', 'host_runtime_sha256')
VERDICTS = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR', 'NOT_APPLICABLE')


class APIRejected(RuntimeError):
    """The API refused the request as invalid (4xx other than authentication); nothing ran."""


class Authoring:
    def program_path(self, version):
        if not isinstance(version, str) or not re.fullmatch('[a-f0-9]{64}', version):
            raise ValueError('diagnostic_version must be a SHA-256 content address')
        return self.output / 'programs' / f'{version}.json'

    def retain_program(self, result, expected_version=None):
        with program_lock(self.program_path(result['diagnostic_version'])):
            return self._retain_program_checked(result, expected_version)

    def _retain_program_checked(self, result, expected_version=None):
        version = result['diagnostic_version']
        path = self.program_path(version)
        if expected_version is not None and version != expected_version:
            raise ValueError('API returned a different DiagnosticVersion')
        for field in ('source_sha256', 'adapter_sha256') + SDK_IDENTITIES:
            if not re.fullmatch('[a-f0-9]{64}', result[field]):
                raise ValueError('API returned an invalid program identity')
        self.sdk_source(result)
        if hashlib.sha256(result['source'].encode()).hexdigest() != result['source_sha256']:
            raise ValueError('compiled source integrity check failed')
        if not isinstance(result['behavior_contract'].get('clauses'), list):
            raise ValueError('API returned an invalid Behavior Contract')
        if path.exists():
            previous = json.loads(path.read_text())
            if (result.get('compilation_context') and previous.get('compilation_context')
                    and result['compilation_context'] != previous['compilation_context']):
                raise ValueError('immutable program compilation context changed')
            for field in ('source_sha256', 'adapter_sha256', 'behavior_contract', 'sdk_source') + SDK_IDENTITIES:
                if previous[field] != result[field]:
                    raise ValueError('immutable program identity changed')
            result = dict(result)
            for field in ('acceptance_id', 'acceptance_receipt', 'test_receipt', 'teaching_receipt',
                          'compilation_scope', 'compilation_context', 'regression_report'):
                if previous.get(field):
                    result[field] = previous[field]
        private_json(path, result)
        return result

    @staticmethod
    def identities(value):
        result = {key: value.get(key) for key in SDK_IDENTITIES}
        if any(not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest)
               for digest in result.values()):
            raise ValueError('SDK/profile/runtime identities are missing or invalid')
        return result

    def check_identities(self, actual, expected):
        if self.identities(actual) != self.identities(expected):
            raise ValueError('SDK/profile/runtime identity mismatch; read get_behavior_sdk and recompile')

    @staticmethod
    def sdk_source(value):
        source = value.get('sdk_source')
        repository = 'https://github.com/cardinalhq/behavior-sdk'
        if (not isinstance(source, dict) or source.get('repository') != repository
                or not re.fullmatch('[a-f0-9]{40}', source.get('commit', ''))
                or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', source.get('version', ''))
                or source.get('sdk_runtime_sha256') != value.get('sdk_runtime_sha256')):
            raise ValueError('public SDK source/version identity is missing or invalid')
        url = f"{repository}/releases/download/v{source['version']}/behavior-sdk-{source['sdk_runtime_sha256']}.json"
        if source.get('artifact_url') != url:
            raise ValueError('public SDK artifact URL does not match its version/digest')
        return source

    def check_sdk_source(self, actual, expected):
        if self.sdk_source(actual) != self.sdk_source(expected):
            raise ValueError('public SDK source/version identity mismatch')

    def sdk(self):
        result = self.request('GET', '/api/v1/behavior-programs/sdk')
        self.identities(result)
        source = self.sdk_source(result)
        artifact = result.get('artifact')
        if (not isinstance(artifact, dict) or artifact.get('format') != 'behavior-sdk-authoring-v1'
                or not isinstance(artifact.get('files'), dict) or not artifact['files']
                or any(not isinstance(path, str) or not isinstance(source, str)
                       for path, source in artifact['files'].items())):
            raise ValueError('API did not return the canonical SDK authoring artifact')
        if result.get('sdk_version') != source['version'] or artifact.get('version') != source['version']:
            raise ValueError('public SDK artifact version mismatch')
        artifact_text = json.dumps(artifact, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        artifact_bytes = artifact_text.encode('utf-8')
        digest = hashlib.sha256(artifact_bytes).hexdigest()
        if result['sdk_runtime_sha256'] != digest or result.get('sdk_artifact_sha256') != digest:
            raise ValueError('SDK authoring artifact integrity check failed')
        path = self.output / 'sdk' / f'{digest}.json'
        if path.exists():
            if path.read_bytes() != artifact_bytes:
                raise ValueError('cached SDK authoring artifact integrity check failed')
        else:
            private_text(path, artifact_text)
        directory = self.output / 'sdk' / digest
        if directory.is_symlink():
            raise ValueError('SDK source cache must not be a symlink')
        files = {}
        for name, content in artifact['files'].items():
            relative = PurePosixPath(name)
            if (relative.is_absolute() or '..' in relative.parts or '\\' in name
                    or relative.as_posix() != name or not relative.parts):
                raise ValueError('SDK artifact contains an invalid source path')
            source_path = directory / name
            if source_path.is_symlink() or not source_path.resolve().is_relative_to(directory.resolve()):
                raise ValueError('SDK source cache contains a symlink or escaping path')
            if source_path.exists():
                if source_path.read_bytes() != content.encode('utf-8'):
                    raise ValueError('cached SDK source integrity check failed')
            else:
                private_text(source_path, content)
            files[name] = str(source_path)
        self.sdk_observation = result
        # Put paginatable source paths first, before the potentially large MCP body.
        return dict(sdk_artifact_file=str(path), sdk_directory=str(directory), sdk_files=files, **result)

    def authoring_identities(self):
        if self.sdk_observation is None:
            raise ValueError('Read get_behavior_sdk before authoring or compiling a candidate')
        return self.identities(self.sdk_observation)

    def compile(self, description, service_name, udf_source, compile_plan, teaching_examples=None):
        if not isinstance(description, str) or not description.strip():
            raise ValueError('description must be nonempty')
        if not isinstance(service_name, str) or not service_name.strip():
            raise ValueError('service_name must be nonempty')
        if not isinstance(udf_source, str) or not udf_source.strip() or not isinstance(compile_plan, dict):
            raise ValueError('udf_source and compile_plan must contain the authored candidate')
        identities = self.authoring_identities()
        teaching_examples = [] if teaching_examples is None else teaching_examples
        if not isinstance(teaching_examples, list) or len(teaching_examples) > 8:
            raise ValueError('provide at most 8 authored teaching examples')
        result = self.request('POST', '/api/v1/behavior-programs/compile', {
            'description': description, 'service_name': service_name,
            'udf_source': udf_source, 'compile_plan': compile_plan,
            'teaching_examples': teaching_examples, **identities})
        if result.get('error') and not result.get('diagnostic_version'):
            return result
        self.check_identities(result, identities)
        self.check_sdk_source(result, self.sdk_observation)
        # Same contract/service repairs inherit all development traces, including
        # failed batches. Compilation creates a proposal, never an incumbent.
        result['compilation_context'] = {'description': description, 'service_name': service_name}
        result['compilation_scope'] = digest(result['compilation_context'])
        return self.retain_program(result)

    def test(self, diagnostic_version, trace_ids, service_name, start, end, expected_verdicts=None, udf_source=None):
        path = self.program_path(diagnostic_version)
        if not path.exists():
            raise ValueError('Compile or inspect this candidate before testing it')
        program = json.loads(path.read_text())
        if udf_source is not None and udf_source != program['source']:
            raise ValueError('source differs from compiled candidate; compile the revised source before testing')
        if (not isinstance(trace_ids, list) or not 1 <= len(trace_ids) <= 8
                or any(not isinstance(t, str) or not re.fullmatch('[a-f0-9]{32}', t) for t in trace_ids)
                or len(set(trace_ids)) != len(trace_ids)):
            raise ValueError('provide 1 to 8 distinct real trace IDs (32 lowercase hex characters)')
        expected_verdicts = {} if expected_verdicts is None else expected_verdicts
        if (not isinstance(expected_verdicts, dict) or set(expected_verdicts) - set(trace_ids)
                or any(v not in ('MATCH', 'NON_MATCH', 'UNKNOWN', 'NOT_APPLICABLE') for v in expected_verdicts.values())):
            raise ValueError('expected_verdicts must map selected trace IDs to MATCH, NON_MATCH, UNKNOWN or NOT_APPLICABLE')
        history = (RegressionHistory(self.output, program['compilation_scope'])
                   if program.get('compilation_scope') else None)
        token = (history.reserve(diagnostic_version, service_name, start, end, trace_ids, expected_verdicts)
                 if history else None)
        try:
            result = self.request('POST', '/api/v1/behavior-programs/test', {
                'diagnostic_version': diagnostic_version, 'trace_ids': trace_ids,
                'service_name': service_name, 'start': start, 'end': end,
                'expected_verdicts': expected_verdicts, **self.identities(program)})
        except APIRejected:
            if history:
                history.release(diagnostic_version, token)
            raise
        self.check_identities(result, program)
        if (result.get('diagnostic_version') != diagnostic_version
                or not re.fullmatch('[a-f0-9]{64}', result.get('test_receipt', ''))):
            raise ValueError('teaching test receipt identity mismatch')
        results = result.get('results', [])
        if (len(results) != len(trace_ids) or {r.get('trace_id') for r in results} != set(trace_ids)
                or any(r.get('verdict') not in VERDICTS for r in results)):
            raise ValueError('teaching test results do not match the requested traces')
        for item in results:
            identity = item.get('execution_identity', {})
            self.check_identities(identity, program)
            self.check_sdk_source(identity, program)
            if (identity.get('diagnostic_version') != diagnostic_version
                    or identity.get('udf_sha256') != program['source_sha256']
                    or identity.get('adapter_sha256') != program['adapter_sha256']):
                raise ValueError('teaching result does not belong to the compiled candidate')
            expected = expected_verdicts.get(item['trace_id'])
            if item.get('expected_verdict') != expected:
                raise ValueError('teaching test expected verdict identity mismatch')
        receipt = result.get('receipt')
        if not isinstance(receipt, dict):
            raise ValueError('teaching test response is missing its immutable receipt')
        digest = hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(',', ':'),
                                           ensure_ascii=False).encode()).hexdigest()
        if digest != result['test_receipt']:
            raise ValueError('teaching test receipt integrity check failed')
        self.check_identities(receipt, program)
        self.check_sdk_source(receipt, program)
        if receipt.get('diagnostic_version') != diagnostic_version or receipt.get('results') != results:
            raise ValueError('teaching test receipt does not match returned results')
        receipt_path = self.output / 'teaching' / f'{digest}.json'
        if receipt_path.exists():
            if json.loads(receipt_path.read_text()) != receipt:
                raise ValueError('cached teaching test receipt integrity check failed')
        else:
            private_json(receipt_path, receipt)
        def persist_program():
            with program_lock(path):
                current = json.loads(path.read_text())
                current.update(test_receipt=result['test_receipt'], teaching_receipt=result)
                private_json(path, current)
        if history:
            history.record(diagnostic_version, service_name, start, end, result, token, persist_program)
        else:
            persist_program()
        return dict(receipt_file=str(receipt_path), **result)

    def inspect_program(self, diagnostic_version):
        self.program_path(diagnostic_version)
        result = self.request('GET', f'/api/v1/behavior-programs/{diagnostic_version}')
        return self.retain_program(result, diagnostic_version)

    def accept(self, diagnostic_version):
        path = self.program_path(diagnostic_version)
        if not path.exists():
            raise ValueError('Inspect the Behavior Contract and source before accepting this version')
        program = json.loads(path.read_text())
        if not program.get('compilation_scope'):
            raise ValueError('Recompile through compile_behavior to register the development regression suite')
        history = RegressionHistory(self.output, program['compilation_scope'])
        with history.locked(), program_lock(path):
            program = json.loads(path.read_text())
            if not program.get('test_receipt'):
                raise ValueError('Run test_behavior on real teaching traces before accepting this version')
            teaching = program['teaching_receipt']['results']
            if any(not item.get('expected_verdict') for item in teaching):
                raise ValueError('Run test_behavior with an expected verdict for every teaching trace before acceptance')
            if any(item['expected_verdict'] != item['verdict'] or item['verdict'] == 'ERROR' for item in teaching):
                raise ValueError('Teaching actual verdicts do not match expectations; inspect evidence and revise before acceptance')
            state = history.read()
            report = history.check(diagnostic_version, state)
            result = self._accept_checked(diagnostic_version, program)
            program['regression_report'] = report
            private_json(path, program)
            history.accepted(diagnostic_version, report, state)
            return result

    def compilation_status(self, diagnostic_version):
        program = json.loads(self.program_path(diagnostic_version).read_text())
        if not program.get('compilation_scope'):
            raise ValueError('Compile this candidate to register its compilation scope')
        return RegressionHistory(self.output, program['compilation_scope']).status(diagnostic_version)

    def run_regressions(self, diagnostic_version):
        """Execute every accumulated real-trace case, even after a failed batch."""
        program = json.loads(self.program_path(diagnostic_version).read_text())
        if not program.get('compilation_scope'):
            raise ValueError('Compile this candidate to register its compilation scope')
        history = RegressionHistory(self.output, program['compilation_scope'])
        cases = history.begin_replay(diagnostic_version)
        groups = {}
        for case in cases:
            locator = case['locator']
            groups.setdefault((locator['service_name'], locator['start'], locator['end']), []).append(case)
        batches, failures = [], []
        for (service, start, end), group in groups.items():
            for offset in range(0, len(group), 8):
                batch = group[offset:offset + 8]
                ids = [case['locator']['trace_id'] for case in batch]
                try:
                    result = self.test(diagnostic_version, ids, service, start, end,
                        {case['locator']['trace_id']: case['expected'] for case in batch})
                    batches.append(result['test_receipt'])
                except Exception as exc:
                    failures.append({'trace_ids': ids, 'reason': f'{type(exc).__name__}: {exc}'})
        try:
            with history.locked():
                report = history.check(diagnostic_version)
        except (ValueError, OSError) as exc:
            report = {'status': 'CANNOT_PROMOTE', 'reason': str(exc)}
        if failures:
            report = {'status': 'CANNOT_PROMOTE', 'reason': 'One or more regression batches failed'}
        return {**report, 'diagnostic_version': diagnostic_version,
                'attempted_cases': len(cases), 'test_receipts': batches, 'failures': failures,
                'incumbent': history.status(diagnostic_version)['incumbent']}

    def _accept_checked(self, diagnostic_version, program):
        result = self.request('POST', f'/api/v1/behavior-programs/{diagnostic_version}/accept',
                              {**self.identities(program), 'test_receipt': program['test_receipt']})
        self.check_identities(result, program)
        self.check_sdk_source(result, program)
        if result.get('diagnostic_version') != diagnostic_version or not result.get('acceptance_id'):
            raise ValueError('acceptance identity mismatch')
        if result.get('test_receipt') != program['test_receipt']:
            raise ValueError('acceptance teaching receipt identity mismatch')
        program['acceptance_id'] = result['acceptance_id']
        program['acceptance_receipt'] = result
        return result
