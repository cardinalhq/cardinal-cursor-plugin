"""Bounded offline qualification; gold never changes a runtime verdict."""
from dataclasses import dataclass, asdict
from pathlib import Path
from copy import deepcopy
import collections
import math
import shutil
import re
from .storage import digest, read, write, filehash, Journal
from .runtime import IDENTITY_KEYS

LABELS = ('MATCH', 'NON_MATCH', 'UNKNOWN')


class RepairFailure(Exception):
    """A recorded invalid repair consumes a slot and may receive bounded feedback."""
    def __init__(self, details):
        self.details=details
        super().__init__('repair failed compilation')


@dataclass(frozen=True)
class Policy:
    max_repairs: int = 2
    audit_per_label: int = 4
    lifetime_executions: int = 10000

    def __post_init__(self):
        if self.max_repairs < 0 or self.audit_per_label < 1 or self.lifetime_executions < 1:
            raise ValueError('invalid qualification policy')


def gate(rows, minimum_per_label=1):
    counts = collections.Counter(r['expected'] for r in rows)
    passed = (all(counts[x] >= minimum_per_label for x in LABELS)
              and all(r['actual'] == r['expected'] and r['mechanical_pass'] for r in rows))
    return {'passed': bool(passed), 'cases': len(rows), 'label_counts': dict(counts),
            'correct': sum(r['actual'] == r['expected'] for r in rows),
            'errors': sum(r['actual'] == 'ERROR' for r in rows),
            'minimum_per_label': minimum_per_label,
            'mismatches': [{'id': r['id'], 'expected': r['expected'], 'actual': r['actual'],
                            'mechanical_pass': r['mechanical_pass']} for r in rows
                           if r['actual'] != r['expected'] or not r['mechanical_pass']]}


def promotion_gate(incumbent_rows, proposal_rows):
    """A repair must execute every regression case without losing any correct case."""
    def identity(row):
        return row['id'], row['trace_sha256'], row['expected']
    same_suite = (bool(incumbent_rows)
                  and len({r['id'] for r in incumbent_rows}) == len(incumbent_rows)
                  and [identity(r) for r in incumbent_rows] == [identity(r) for r in proposal_rows])
    failures = [r['id'] for r in proposal_rows
                if r['actual'] not in LABELS or not r['mechanical_pass']]
    regressions = [old['id'] for old, new in zip(incumbent_rows, proposal_rows)
                   if old['actual'] == old['expected'] and old['mechanical_pass']
                   and (new['actual'] != new['expected'] or not new['mechanical_pass'])]
    return {'passed': same_suite and not failures and not regressions,
            'same_suite': same_suite, 'cases': len(proposal_rows),
            'execution_failures': failures, 'regressions': regressions,
            'incumbent_versions': sorted({r['version'] for r in incumbent_rows}),
            'proposal_versions': sorted({r['version'] for r in proposal_rows})}


def case_fingerprint(case):
    # Ignore occurrence identifiers so renaming a training trace does not make it new.
    trace=case['trace']
    encoded=__import__('json').dumps(trace,sort_keys=True,ensure_ascii=False)
    replacements={trace.get('trace_id',''):'__trace__'}
    replacements.update({e['id']:f'__event_{i}__' for i,e in enumerate(trace.get('events',[]))})
    for prefix in ('toolu_','msg_'):
        for i,value in enumerate(dict.fromkeys(re.findall(prefix+r'[A-Za-z0-9_-]+',encoded))):
            replacements[value]=f'__{prefix}{i}__'
    for old,new in sorted(replacements.items(),key=lambda kv:-len(kv[0])):
        if old:encoded=encoded.replace(old,new)
    return digest(encoded.encode())


def check_cases(cases, used_ids=(), used_hashes=()):
    ids, hashes = set(used_ids), set(used_hashes)
    for case in cases:
        if case['expected'] not in LABELS or not case.get('family'):
            raise ValueError('explicit three-class gold and semantic family required')
        key = case_fingerprint(case)
        if case['id'] in ids or key in hashes:
            raise ValueError('case overlap/duplicate')
        if not case.get('review_pass'):
            raise ValueError('case lacks independent blind review')
        ids.add(case['id']); hashes.add(key)
    return ids, hashes


class Qualifier:
    """Injected roles have separate contexts. audit() is invoked once, after selection.

    challenge(candidate, revision) -> independently reviewed cases
    repair(candidate, counterexamples, revision) -> compiled candidate
    evaluate(candidate, trace) -> unmodified SDK output + mechanical/usage fields
    audit() -> previously hidden, independently reviewed cases
    """
    def __init__(self, root, policy, evaluate, challenge, repair, audit):
        self.root = Path(root); self.root.mkdir(parents=True, exist_ok=False)
        self.policy, self.evaluate = policy, evaluate
        self.challenge, self.repair, self.audit = challenge, repair, audit
        self.journal = Journal(self.root / 'executions')
        write(self.root / 'policy.json', asdict(policy))

    def score(self, candidate, cases, phase):
        rows = []
        for case in cases:
            # Only the trace crosses the runtime boundary, never expected/rationale/tags.
            def execute():
                try:
                    return self.evaluate(candidate, case['trace'])
                except Exception as exc:
                    # An evaluator failure must not short-circuit the rest of the suite.
                    reason = f'{type(exc).__name__}: {exc}'
                    return {'actual': 'ERROR', 'mechanical_pass': False, 'reason': reason,
                            'audit': {'errors': [reason]}, 'jev_receipts': [],
                            'metrics': {key: None for key in ('input_tokens', 'output_tokens',
                                        'provider_attempts', 'latency_ms', 'cost_usd')}}
            raw = self.journal.call([phase, case['id']],
                                    {'version': candidate['version'], 'trace_sha256': digest(case['trace'])},
                                    execute)
            rows.append({**raw, 'id': case['id'], 'family': case['family'],
                         'expected': case['expected'], 'version': candidate['version'],
                         'trace_sha256': digest(case['trace'])})
        write(self.root / (phase + '.json'), rows)
        return rows

    def run(self, initial, discovery):
        candidate = deepcopy(initial)
        cases = list(discovery)
        used_ids, used_hashes = check_cases(cases)
        write(self.root / 'discovery.json', cases)
        counterexamples=[]
        incumbent_rows=[]
        for revision in range(self.policy.max_repairs + 1):
            proposal = candidate
            if revision:
                try:
                    proposal=self.repair(deepcopy(candidate),counterexamples,revision)
                except RepairFailure as exc:
                    write(self.root/f'compile-failure-{revision}.json',exc.details)
                    counterexamples=[x for x in counterexamples if 'compilation_failure' not in x]
                    counterexamples.append({'compilation_failure':exc.details})
                    if revision==self.policy.max_repairs:
                        return self.finish(candidate,'CANNOT_QUALIFY','repair_compile_budget_exhausted',revision)
                    continue
                write(self.root / f'proposal-{revision}.json', proposal)
            else:
                write(self.root / 'candidate-0.json', candidate)
            fresh = self.challenge(deepcopy(proposal), revision)
            used_ids, used_hashes = check_cases(fresh, used_ids, used_hashes)
            cases.extend(fresh)
            write(self.root / f'challenges-{revision}.json', fresh)
            if revision:
                # Retain incumbent observations on old cases; extend its baseline on
                # every new case before comparing on the complete accumulated suite.
                incumbent_rows += self.score(candidate, fresh, f'incumbent-new-{revision}')
                write(self.root / f'incumbent-baseline-{revision}.json', incumbent_rows)
            rows = self.score(proposal, cases, f'development-{revision}')
            promoted = True
            if revision:
                promotion = promotion_gate(incumbent_rows, rows)
                write(self.root / f'promotion-{revision}.json', promotion)
                promoted = promotion['passed']
                if promoted:
                    candidate = deepcopy(proposal)
                # Exception recovery reads these manifests. Rejected proposals must
                # never appear here, even when the repair budget is exhausted.
                write(self.root / f'candidate-{revision}.json', candidate)
            if promoted:
                incumbent_rows = rows
            result = gate(rows)
            write(self.root / f'development-gate-{revision}.json', result)
            if promoted and result['passed']:
                # One untouched qualification set; failing it is terminal for this cycle.
                audit = self.audit()
                check_cases(audit, used_ids, used_hashes)
                write(self.root / 'audit-cases.json', audit)
                audit_rows = self.score(candidate, audit, 'audit')
                final_gate = {**gate(audit_rows, self.policy.audit_per_label),
                              'version': candidate['version'], 'development_gate': result,
                              'audit_sha256': digest(audit), 'policy_sha256': digest(asdict(self.policy))}
                write(self.root / 'qualification.json', final_gate)
                return self.finish(candidate, 'QUALIFIED' if final_gate['passed'] else
                                   'CANNOT_QUALIFY', 'qualification_gate', revision)
            if revision == self.policy.max_repairs:
                return self.finish(candidate, 'CANNOT_QUALIFY', 'repair_budget_exhausted', revision)
            by_id = {c['id']: c for c in cases}
            counterexamples = [{'case': by_id[row['id']], 'execution': row} for row in rows
                               if row['actual'] != row['expected'] or not row['mechanical_pass']]
            write(self.root / f'counterexamples-{revision}.json', counterexamples)
        raise AssertionError('unreachable')

    def finish(self, candidate, status, reason, revision):
        result = {'status': status, 'reason': reason, 'repairs': revision, 'candidate': candidate}
        write(self.root / 'result.json', result)
        return result


def freeze(bundle, result, identity, qualification_file, dependencies):
    if result['status'] != 'QUALIFIED':
        raise ValueError('cannot deploy unqualified candidate')
    candidate = result['candidate']
    identity = {**identity, 'program': filehash(candidate['source'])}
    if set(identity) != set(IDENTITY_KEYS) or not all(identity.values()):
        raise ValueError('all lifecycle fingerprints are required')
    qualification = read(qualification_file)
    if not qualification['passed'] or qualification['version'] != candidate['version']:
        raise ValueError('gate/version mismatch')
    bundle = Path(bundle); bundle.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(candidate['source'], bundle / 'program.py')
    shutil.copyfile(qualification_file, bundle / 'qualification.json')
    for name, path in dependencies.items():
        target = bundle / name
        if not target.resolve().is_relative_to(bundle.resolve()) or target.exists():
            raise ValueError('invalid dependency destination')
        target.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(path, target)
    files = {str(p.relative_to(bundle)): filehash(p) for p in bundle.rglob('*') if p.is_file()}
    certificate = {'status': 'QUALIFIED', 'version': candidate['version'], 'identity': identity,
                   'files': files, 'qualification_sha256': digest(qualification),
                   'production_path': 'existing SDK runtime and embedded JEV only'}
    certificate['certificate_sha256'] = digest(certificate)
    write(bundle / 'certificate.json', certificate)
    return certificate


def cost_summary(qualification_rows, execution_rows, lifetime):
    if lifetime <= 0:
        raise ValueError('expected lifetime must be positive')
    fields = ('input_tokens', 'output_tokens', 'provider_attempts', 'latency_ms', 'cost_usd')
    def total(rows, field):
        values = [r.get(field) for r in rows]
        return sum(values) if all(isinstance(v, (int, float)) and math.isfinite(v)
                                  and v >= 0 for v in values) else None
    q = {f: total(qualification_rows, f) for f in fields}
    e = {f: total(execution_rows, f) / len(execution_rows)
         if execution_rows and total(execution_rows, f) is not None else None for f in fields}
    return {'qualification_total': q, 'steady_state_per_execution': e,
            'expected_lifetime_executions': lifetime,
            'qualification_amortized_per_execution': {f: v/lifetime if v is not None else None
                                                      for f, v in q.items()},
            'lifetime_total_usd': q['cost_usd'] + lifetime * e['cost_usd']
            if q['cost_usd'] is not None and e['cost_usd'] is not None else None,
            'amortized_total_usd_per_execution': q['cost_usd']/lifetime + e['cost_usd']
            if q['cost_usd'] is not None and e['cost_usd'] is not None else None,
            'note': 'Summed service latency is accounting, not added online latency. Unknown prices stay null.'}
