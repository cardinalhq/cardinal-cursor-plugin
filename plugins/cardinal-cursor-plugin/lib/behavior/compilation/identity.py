"""Fingerprint actual inputs for deployment admission and explicit requalification."""
from pathlib import Path
from .storage import filehash, digest


def content_manifest(files):
    """Caller chooses the complete relevant file set; missing inputs fail closed."""
    if not files:raise ValueError('identity requires nonempty input manifest')
    return {str(name):filehash(path) for name,path in sorted(files.items())}


def identity_from_files(program, agent_files, telemetry_files, contract_files, sdk_files, jev_config):
    return {'program':filehash(program),'agent':digest(content_manifest(agent_files)),
            'telemetry_schema':digest(content_manifest(telemetry_files)),
            'contract':digest(content_manifest(contract_files)),
            'sdk':digest(content_manifest(sdk_files)),'jev':digest(jev_config)}
