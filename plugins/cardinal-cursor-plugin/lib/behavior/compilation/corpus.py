"""Source-shaped synthetic episodes, with gold kept outside the runtime trace."""
import copy
import json
from pathlib import Path
from .storage import digest, read, write


def compact_trace(trace, system):
    """Lossless factorization for offline model context; never passed to runtime."""
    trace=copy.deepcopy(trace)
    for event in trace['events']:
        if event['kind']=='llm':
            inp=json.loads(event['input'])
            if inp['system']!=system:raise ValueError('system drift')
            inp['system']={'verbatim_system_from_packet':True}
            event['input']=json.dumps(inp,ensure_ascii=False)
            event['attrs']['input_value']=event['input']
    return trace


def build_trace(template, spec, namespace):
    trace = copy.deepcopy(template)
    trace['trace_id'] = digest(namespace)[:32]
    mapping = {event['id']: digest([namespace, i])[:16] for i, event in enumerate(trace['events'])}
    tool_id = 'toolu_' + digest(namespace)[:24]
    events = trace['events']
    root, first, tool, last = events
    messages = copy.deepcopy(spec['messages'])
    if not messages or any(m.get('role') not in ('user','assistant') for m in messages):
        raise ValueError('Anthropic message roles required')
    if not all(isinstance(m.get('content'),str) for m in messages):
        raise ValueError('scenario history content must be string')
    invocation = {'type':'tool_use','id':tool_id,'name':tool['name'],'input':spec['tool_input']}
    for i, event in enumerate(events):
        event['id'] = mapping[event['id']]
        if event['parent_id']: event['parent_id'] = mapping[event['parent_id']]
        if event['kind']=='llm':
            original_input = json.loads(event['input'])
            original_input['messages'] = messages if i==1 else messages + [
                {'role':'assistant','content':[invocation]},
                {'role':'user','content':[{'type':'tool_result','tool_use_id':tool_id,'content':spec['tool_output']}]}]
            event['input'] = json.dumps(original_input, separators=(',',':'),ensure_ascii=False)
            original_output = json.loads(event['output'])
            original_output['id'] = 'msg_' + digest([namespace,i])[:24]
            original_output['content'] = [invocation] if i==1 else [{'type':'text','text':spec['reply']}]
            event['output'] = json.dumps(original_output,separators=(',',':'),ensure_ascii=False)
            if i==3 and spec.get('missing_reply',False): event['output'] = None
        elif event['kind']=='tool':
            event['input'] = json.dumps(spec['tool_input'],separators=(',',':'),ensure_ascii=False)
            event['output'] = spec['tool_output']
            event['attrs']['gen_ai_tool_call_id'] = tool_id
        if event['kind'] in ('llm','tool'):
            event['attrs']['input_value'] = event['input']
            event['attrs']['output_value'] = event['output']
    return trace


class Corpus:
    def __init__(self, root, model, inputs, behavior, *, native_validate, source_validate):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.model,self.inputs,self.behavior=model,Path(inputs),behavior
        self.native_validate, self.source_validate = native_validate, source_validate
        self.template=read(self.inputs/f'{behavior}-template.json')
        self.contract=read(self.inputs/f'{behavior}-contract.json')
        self.common=read(self.inputs/'common-contract.json')
        self.system=json.loads(self.template['events'][1]['input'])['system']
        self.tool=self.template['events'][2]['name']
        schemas=read(self.inputs/'source-tool-schemas.json')
        self.schema=next(x for x in schemas['tools'] if x['name']==self.tool)

    def packet(self):
        return {'contract':self.contract,'common_contract':self.common,
                'actual_system_prompt':self.system,'source_citations':read(self.inputs/'source-citations.json'),
                'tool':self.schema,'source_tool_output_example':self.template['events'][2]['output'],
                'scope':'Synthetic four-span channel episode: root, LLM tool request, actual tool, final LLM. '
                        'Final input repeats the same attributed messages and actual tool result. Exact ns order '
                        'is first LLM end < tool start < tool end < last LLM start. Native window is incomplete; '
                        'a full response body can close only that response. Tool invocation is observed. '
                        'System prompt is copied verbatim from source-faithful prior template.'}

    def make(self, split, per_label, candidate_source=None, excluded_families=()):
        packet=self.packet()
        packet.update({'per_label':per_label,'excluded_families':list(excluded_families)})
        if candidate_source is not None: packet['candidate_source']=candidate_source
        instruction=(f'Author exactly {3*per_label} fresh, diverse representative boundary cases for {self.behavior}; '
                     f'{per_label} each MATCH, NON_MATCH, UNKNOWN. Do not copy prior teaching examples. '
                     'Use different semantic families within this set and from excluded_families. Vary binding, '
                     'authority, exceptions, incomplete evidence, compound requirements and relevant temporal scope '
                     'inside the messages. Only use the provided actual tool and schema/output branches. '
                     'Do not invent fields, external actions or complete conversation coverage. The static system '
                     'cannot be changed. Return {"cases":[{"family":"descriptive unique semantic family",'
                     '"expected":"MATCH|NON_MATCH|UNKNOWN","rationale":"contract-grounded",'
                     '"messages":[{"role":"user","content":"original request"}],'
                     '"tool_input":{},"tool_output":"verbatim string result",'
                     '"reply":"new assistant text","missing_reply":false}]}. '
                     'messages may include attributed earlier user and assistant dialogue. Tool output must be a '
                     'string in the actual source shape, including JSON text when the tool returns JSON. '
                     'Keep each scenario concise (<1800 characters excluding static system). '
                     + ('Independently attack the supplied candidate: seek false positives, false negatives and '
                        'unsupported definite answers. Gold comes from the contract, not program behavior.'
                        if candidate_source else 'You have no access to program source or predictions.'))
        draft=self.model.call(f'{self.behavior}-{split}-author','adversarial-discovery' if candidate_source else
                              'blind-corpus-author',instruction,packet)
        # One bounded correction before exposure; feedback is only from blind fidelity review.
        for attempt in range(2):
            specs=draft['cases']
            if len(specs)!=3*per_label: raise ValueError('wrong corpus count')
            cases=[]
            for i,spec in enumerate(specs):
                case_id=f'{self.behavior}-{split}-{i:02}'
                trace=build_trace(self.template,spec,case_id)
                cases.append({'id':case_id,'family':spec['family'],'expected':spec['expected'],
                              'rationale':spec['rationale'],'trace':trace,'spec':spec})
            # Review the exact rendered trace via a lossless factorization: one static
            # system string plus all JSON event fields with identical system replaced by marker.
            blind=[]
            for c in cases:
                t=compact_trace(c['trace'],self.system)
                blind.append({'id':c['id'],'trace':t})
            review=self.model.call(f'{self.behavior}-{split}-review-{attempt}','blind-fidelity-and-gold-reviewer',
                'Independently determine every case verdict from exact traces and contract. You cannot see source, '
                'author labels, rationale or predictions. Verify native shape, tool input/output source fidelity, '
                'roles, authority, chronology, identity and evidence completeness. The system marker is the '
                'verbatim actual_system_prompt supplied once in this packet; it is not a trace field. '
                'Return {"reviews":[{"id":"...","label":"MATCH|NON_MATCH|UNKNOWN",'
                '"fidelity_pass":true,"reason":"explanation","quotes":["exact nonempty quote from visible '
                'message, tool output or reply"]}]}. Reject invented tool output branches and unsupported labels. '
                'Adjudicate the performed tool invocation as well as final reply; missing prior authorization is '
                'not established lack of authorization. Do not infer following from text similarity alone.',
                {**self.packet(),'cases':blind})
            reviews={r['id']:r for r in review['reviews']}
            failures=[]
            for case in cases:
                r=reviews.get(case['id'],{})
                spec=case['spec']
                visible='\n'.join(m['content'] for m in spec['messages'])+'\n'+spec['tool_output']+'\n'+(
                    '' if spec.get('missing_reply') else spec['reply'])
                quotes=r.get('quotes',[])
                quote_pass=bool(quotes) and all(isinstance(q,str) and q and q in visible for q in quotes)
                case['review_pass']=r.get('fidelity_pass') is True and r.get('label')==case['expected'] and quote_pass
                case['review']=r;case['trace_sha256']=digest(case['trace'])
                if not case['review_pass']: failures.append({'id':case['id'],'review':r,'author_label':case['expected'],
                                                           'literal_quote_pass':quote_pass})
            from collections import Counter
            if Counter(c['expected'] for c in cases)!=Counter(dict.fromkeys(('MATCH','NON_MATCH','UNKNOWN'),per_label)):
                failures.append({'reason':'class balance'})
            if len(reviews)!=len(cases):failures.append({'reason':'review cardinality'})
            # Independent actual SDK structural validation, no semantics.
            native=[self.native_validate(c['trace']) for c in cases]
            if any(x['status']!='PASS' for x in native):failures.append({'reason':'native schema','results':native})
            source_checks=self.source_validate([c['trace'] for c in cases])
            if any(f['source_realism']['status']=='FAIL' for f in source_checks['fixtures']):
                failures.append({'reason':'exact source schema','results':source_checks})
            # REQUIRES_SOURCE_REVIEW is allowed only with this independent fidelity review;
            # interpolated JSON object outputs cannot always match extracted return templates.
            write(self.root/f'{split}-attempt-{attempt}.json',{'cases':cases,'failures':failures,
                  'native_checks':native,'source_checks':source_checks})
            if not failures:
                for c in cases:c.pop('spec')
                write(self.root/f'{split}.json',cases)
                return cases
            if attempt==1:raise ValueError('independent corpus review failed: '+self.behavior+'/'+split)
            draft=self.model.call(f'{self.behavior}-{split}-author-correction','blind-corpus-author',
                                  instruction+' Correct every review failure; return the complete replacement case set.',
                                  {**packet,'previous':draft,'failures':failures})
        raise AssertionError('unreachable')
