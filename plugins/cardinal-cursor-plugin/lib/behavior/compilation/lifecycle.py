"""Explicit offline requalification and optional evidence collection.

Neither operation is imported or required by the production executor.
"""
import datetime
from pathlib import Path
from .storage import read, write, filehash, digest
from .runtime import load_frozen, requalification_reasons
from .engine import Qualifier, freeze


def collect_sample(queue, trace, certificate_sha256, reason):
    """Persist already-collected telemetry; no model, verdict replacement, or sampling mandate.

    The calling application owns sampling frequency, data minimization and retention.
    Call asynchronously outside the execution path if collection latency matters.
    """
    key=digest({'trace':trace,'certificate':certificate_sha256})
    write(Path(queue)/(key+'.json'),{'trace':trace,'certificate_sha256':certificate_sha256,
          'reason':reason,'purpose':'evidence for a future explicit qualification cycle',
          'collected_at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
    return key


def requalify(root, parent_bundle, observed, reason, initial, discovery, policy,
              evaluate, challenge, repair, audit, dependencies):
    """Run a fresh bounded loop, export only on pass, retain immutable parent lineage.

    observed fingerprints must be independently computed from current inputs. All role
    callbacks use new contexts/corpora. Previously exposed traces may be regression
    cases, but audit() must supply a genuinely new, untouched qualification set.
    dependencies(candidate) returns portable SDK artifact files for the final version.
    """
    if not reason.strip():raise ValueError('explicit requalification reason required')
    parent_bundle=Path(parent_bundle)
    parent=read(parent_bundle/'certificate.json')
    load_frozen(parent_bundle,parent['identity'])  # validate parent, not admit changed deployment
    if observed.get('program')!=filehash(initial['source']):
        raise ValueError('observed program does not match new source')
    root=Path(root);root.mkdir(parents=True,exist_ok=False)
    request={'reason':reason,'parent_certificate_sha256':parent['certificate_sha256'],
             'parent_version':parent['version'],'current_identity':observed,
             'changed_fields':requalification_reasons(parent,observed),
             'requested_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    write(root/'request.json',request)
    result=Qualifier(root/'qualification',policy,evaluate,challenge,repair,audit).run(initial,discovery)
    write(root/'outcome.json',result)
    if result['status']=='QUALIFIED':
        current={k:v for k,v in observed.items() if k!='program'}
        deps={**dependencies(result['candidate']),'lineage.json':root/'request.json'}
        freeze(root/'bundle',result,current,root/'qualification/qualification.json',deps)
    return result
