"""Deployment boundary: hashes, lifecycle admission, then existing SDK execution.

No LLM transport, discovery, review, repair, or gold labels are imported here.
The caller supplies the unchanged SDK runner with its embedded JEV factory.
"""
from .storage import digest, read, filehash
from pathlib import Path

IDENTITY_KEYS = ('program', 'agent', 'telemetry_schema', 'contract', 'sdk', 'jev')


def requalification_reasons(certificate, observed):
    expected = certificate['identity']
    return [key for key in IDENTITY_KEYS if not observed.get(key) or
            observed.get(key) != expected.get(key)]


def load_frozen(bundle, observed):
    bundle = Path(bundle)
    certificate = read(bundle / 'certificate.json')
    claimed = certificate.pop('certificate_sha256')
    if digest(certificate) != claimed:
        raise ValueError('certificate changed')
    if certificate['status'] != 'QUALIFIED':
        raise ValueError('program is not qualified')
    for name, sha in certificate['files'].items():
        path = (bundle / name).resolve()
        if not path.is_relative_to(bundle.resolve()) or filehash(path) != sha:
            raise ValueError('frozen dependency changed: ' + name)
    source = (bundle / 'program.py').read_text()
    if digest(source.encode()) != certificate['identity']['program']:
        raise ValueError('program identity mismatch')
    gate = read(bundle / 'qualification.json')
    if not gate['passed'] or gate['version'] != certificate['version']:
        raise ValueError('qualification does not bind this version')
    reasons = requalification_reasons(certificate, observed)
    if reasons:
        raise ValueError('explicit requalification required: ' + ', '.join(reasons))
    return certificate, source


def execute(bundle, observed, trace, sdk_runner):
    certificate, source = load_frozen(bundle, observed)
    return sdk_runner(certificate['version'], source, trace)
