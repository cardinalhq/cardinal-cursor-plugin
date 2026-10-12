"""Configurable adapter to the existing SDK compiler and embedded-JEV runtime.

The application supplies its pinned SDK import environment and existing profile,
JEV factory, sandbox runner and evidence auditor. No SDK or prompts are vendored.
"""
import dataclasses
import functools
import time
from pathlib import Path
from .engine import RepairFailure
from .storage import read, write, digest


class SDK:
    def __init__(self, root, *, profile_factory, judge_factory, program_runner,
                 audit_result, metrics, verify, jev_config, population,
                 evaluation_policy, finality_policy):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile_factory, self.judge_factory = profile_factory, judge_factory
        self.program_runner, self.audit_result, self.metrics = program_runner, audit_result, metrics
        self.verify, self.jev_config, self.population = verify, jev_config, population
        self.evaluation_policy, self.finality_policy = evaluation_policy, finality_policy
        verify()

    def compile(self, name, source, plan, description, context):
        from behavior_runtime.diagnostics import ArtifactStore
        from behavior_runtime.compiler import Compiler, CompilerConfig, CompileRequest, CompileFailure
        self.verify()
        out = self.root / name
        if not out.resolve().is_relative_to(self.root.resolve()) or out == self.root:
            raise ValueError('invalid compilation name')
        out.mkdir()
        (out / 'program.py').write_text(source)
        write(out / 'plan.json', plan)
        store = ArtifactStore(out / 'artifacts')
        config = CompilerConfig(model='supplied-source', model_version='1', max_revisions=0,
                                max_output_tokens=32768)
        compiler = Compiler(store, lambda _: {
            'model': 'supplied-source', 'model_version': '1',
            'text': __import__('json').dumps({'udf_source': source, 'compile_plan': plan}),
            'usage': {'input_tokens': 0, 'output_tokens': 0}}, self.judge_factory, config,
            program_runner=self.program_runner)
        request = CompileRequest(diagnostic_id=name.split('-')[0], behavior_description=description,
            optional_user_context=context, examples=(), profile=self.profile_factory(),
            jev_config=self.jev_config, population=self.population,
            evaluation_policy=self.evaluation_policy, finality_policy=self.finality_policy)
        try:
            result = compiler.compile(request)
        except CompileFailure as exc:
            receipts = list((out / 'artifacts/compile_receipts').glob('*.json'))
            errors = [error for p in receipts for round in read(p)['rounds'] for error in round['errors']]
            raise RepairFailure({'errors': errors, 'draft': {'udf_source': source, 'compile_plan': plan},
                                 'receipt_paths': [str(p) for p in receipts]}) from exc
        write(out / 'compile-receipt.json', result.receipt)
        descriptor = {'version': result.diagnostic_version.version, 'source': str(out / 'program.py'),
                      'plan': str(out / 'plan.json'), 'store': str(out / 'artifacts')}
        write(out / 'descriptor.json', descriptor)
        return descriptor

    def evaluate(self, candidate, trace):
        from behavior_runtime.diagnostics import ArtifactStore
        self.verify()
        store = ArtifactStore(candidate['store'])
        version = store.version(candidate['version'])
        source = Path(candidate['source']).read_text()
        if digest(source.encode()) != version.source_sha256:
            raise ValueError('source/version binding failed')
        started = time.monotonic()
        try:
            row = dataclasses.asdict(store.evaluate(version.version, trace, profile=self.profile_factory(),
                judge_factory=self.judge_factory, finalized=True,
                _program_runner=functools.partial(self.program_runner, source)))
        except Exception as exc:
            row = {'verdict': 'ERROR', 'reason': f'{type(exc).__name__}: {exc}', 'jev_receipts': [],
                   'records': [], 'witness_refs': []}
        audit = self.audit_result(row, trace)
        usage = self.metrics(row, (time.monotonic() - started) * 1000)
        return {**row, 'actual': row['verdict'], 'mechanical_pass': audit['mechanical_status'] == 'PASS',
                'audit': audit, 'metrics': {**usage, 'cost_usd': None}, 'trace_sha256': digest(trace)}
