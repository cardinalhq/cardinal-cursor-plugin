"""Plugin-owned discover/compile/challenge/repair/qualify/freeze orchestration.

SDK, independently reviewed corpus and stateless role transport are injected.
No experiment directories, model credentials or private agent prompts are bundled.
"""
from dataclasses import asdict
from pathlib import Path
import time
from .corpus import compact_trace
from .engine import Policy, Qualifier, freeze, cost_summary
from .storage import read, write, digest


def compile_and_qualify(root, *, source, plan, description, context, sdk,
                        corpus, model, sdk_documentation, jev_config, identity,
                        dependencies, policy=Policy(), discovery_per_label=3,
                        challenge_per_label=2, usage_rows=lambda: []):
    """One explicit offline cycle; only a passing final version gets a bundle.

    sdk.compile must preserve SDK source/version binding and raise RepairFailure
    for recorded compiler rejections; sdk.evaluate returns unmodified SDK verdicts.
    corpus.make independently reviews each rendered case before returning it.
    dependencies(candidate) supplies portable runtime artifact files for freezing.
    usage_rows() includes role, compile and candidate/audit execution costs; an
    empty collection means unknown cost, not free qualification.
    """
    root=Path(root); root.mkdir(parents=True, exist_ok=False)
    b=corpus.behavior
    write(root/'request.json', {'behavior':b, 'policy':asdict(policy),
          'source_sha256':digest(source.encode()), 'plan_sha256':digest(plan),
          'contract_sha256':digest([description,context]),
          'sdk_documentation_sha256':digest(sdk_documentation.encode()),
          'jev_config_sha256':digest(jev_config),
          'discovery_per_label':discovery_per_label, 'challenge_per_label':challenge_per_label})
    started=time.monotonic()
    initial=None
    try:
        initial=sdk.compile(b+'-initial',source,plan,description,context)
        write(root/'initial.json',initial)
        discovery=corpus.make('discovery',discovery_per_label)
        families=[c['family'] for c in discovery]
        def challenge(candidate,revision):
            cases=corpus.make(f'challenge-{revision}',challenge_per_label,
                             Path(candidate['source']).read_text(),families)
            families.extend(c['family'] for c in cases)
            return cases
        def repair(candidate,counterexamples,revision):
            feedback=[{'id':c['case']['id'],'expected':c['case']['expected'],
                       'review':c['case']['review'],'trace':compact_trace(c['case']['trace'],corpus.system),
                       'actual':c['execution']['actual'],'reason':c['execution']['reason'],
                       'mechanical_audit_errors':c['execution']['audit']['errors'],
                       'jev_answers':[{'proposition':r['proposition'],'decision':r['decision'],'reason':r['reason']}
                                      for r in c['execution'].get('jev_receipts',[])]} for c in counterexamples if 'case' in c]
            compilation_feedback=[c['compilation_failure'] for c in counterexamples if 'compilation_failure' in c]
            response=model.call(f'{b}-repair-{revision}','program-repair',
                'Repair this ordinary Python Behavioral Program against independently discovered counterexamples. '
                'Do not special-case case ids, entities, exact text, or expected labels; implement the behavioral '
                'contract generically. Use only existing SDK runtime with embedded JEV calls, never an external '
                'adjudicator. Preserve source-faithful raw evidence and UNKNOWN on insufficient material evidence. '
                'Do not omit static instructions unless exact known static constants and all dynamic context are '
                'accounted for. All requirements in the contract remain binding. Return only '
                '{"udf_source":"complete Python source","compile_plan":{"schema_version":2,"clauses":[],"judge_sites":[]}}. '
                'Preserve valid clause quotes from description/context/sdk and adjust implementation anchors and '
                'judge sites to real functions. The allowed source_ref for description is "behavior", NOT '
                '"description"; other source_ref values are "context" and "sdk". Every literal JEV proposition '
                'must be 1-1000 characters. Incorporate supplied compiler rejection details before proposing '
                'another candidate. No new imports beyond SDK and allowed Python stdlib.',
                {'source':Path(candidate['source']).read_text(),'compile_plan':read(candidate['plan']),
                 'description':description,'context':context,'contract':corpus.contract,'common':corpus.common,
                 'sdk_documentation':sdk_documentation,'jev_config':jev_config,
                 'actual_system_prompt':corpus.system,'counterexamples':feedback,'compilation_failures':compilation_feedback})
            return sdk.compile(f'{b}-repair-{revision}',response['udf_source'],response['compile_plan'],description,context)
        def audit():
            return corpus.make('audit',policy.audit_per_label,excluded_families=families)
        result=Qualifier(root/'qualification',policy,sdk.evaluate,challenge,repair,audit).run(initial,discovery)
        if result['status']=='QUALIFIED':
            freeze(root/'bundle',result,identity,root/'qualification/qualification.json',
                   dependencies(result['candidate']))
    except Exception as exc:
        candidates=sorted((root/'qualification').glob('candidate-*.json'),
                          key=lambda p:int(p.stem.split('-')[-1]))
        current=read(candidates[-1]) if candidates else initial
        result={'status':'CANNOT_QUALIFY','reason':f'{type(exc).__name__}: {exc}','candidate':current}
    result['wall_latency_ms']=(time.monotonic()-started)*1000
    write(root/'outcome.json',result)
    costs=usage_rows()
    write(root/'cost.json',cost_summary(costs or [{}],[],policy.lifetime_executions))
    return result
