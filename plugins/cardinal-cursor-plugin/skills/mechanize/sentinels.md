The implementation should be optimized around proving structural reuse, not around building a broad workflow platform. The central artifact is an immutable Sentinel DAG; a Variation is a small, local overlay on that DAG.

Mechanize v0 Implementation and Experimentation Specification

1. Product objective

Build an open-source agent skill and local runtime that converts a completed investigation into an executable Sentinel DAG.

A later, related investigation must be able to:

1. Discover the existing Sentinel.
2. Determine whether its investigation procedure is structurally reusable.
3. Create a local Variation over the Sentinel.
4. Execute the resolved Variation.
5. Preserve the base Sentinel unchanged.
6. Optionally publish the base Sentinel or selected Variations to a remote registry.

The core product hypothesis is not:

An LLM can generate a workflow.

The core product hypothesis is:

A reusable investigation procedure can be extracted once, discovered later, adapted through a small overlay, and executed against a different problem.

2. Primary proof required

The implementation is successful only when it produces evidence for both of these claims.

Claim A: investigation compilation

A real investigation can be compiled into a Sentinel DAG that executes independently of the original conversation.

Claim B: investigation variation

A meaningfully different investigation can reuse the same Sentinel DAG through a small Variation rather than generating an unrelated DAG.

Claim B is the primary product bet.

3. Definitions

Investigation

An interactive agent session in which the agent:

* receives an objective;
* gathers evidence;
* invokes tools;
* writes or runs local code;
* evaluates hypotheses;
* reaches a conclusion.

Sentinel

An immutable, typed, executable DAG representing the reusable procedure extracted from an investigation.

A Sentinel contains:

* semantic purpose;
* typed inputs;
* nodes;
* edges;
* output contract;
* execution policies;
* evidence provenance;
* capability requirements;
* variation points;
* validation fixtures.

Variation

A local overlay that adapts a Sentinel without copying its full DAG.

A Variation may modify only declared variation points unless explicitly placed into unsafe mode.

A Variation may:

* bind different inputs;
* replace tool bindings;
* change thresholds;
* change time windows;
* change schedules or triggers;
* change LLM policy;
* insert a node at an extension point;
* disable an optional node;
* replace a node declared replaceable;
* change output routing.

A Variation must reference an immutable Sentinel digest.

Resolved DAG

The executable DAG produced by applying a Variation to its referenced Sentinel.

The resolved DAG is materialized for execution but is not stored as an independent authored Sentinel.

Mechanization

The compilation process that converts an investigation into either:

* a new Sentinel; or
* a Variation of an existing Sentinel.

4. Non-goals for v0

Do not build:

* a visual workflow editor;
* a multi-tenant hosted scheduler;
* a general agent framework;
* a proprietary workflow language unrelated to the experiment;
* automatic support for arbitrary programming languages;
* a marketplace;
* distributed execution;
* complex RBAC;
* automatic secret distribution;
* autonomous remediation;
* an observability-specific runtime requirement.

The local runtime must be sufficient to execute experiments end to end.

Sections §53–§58 describe a v1 productization layer that adds Kubernetes CRDs, a GitOps sentinels repository, CI/CD scaffolding, and skill-driven GitHub publishing. That layer ships only after the §51 evidence gates for v0 pass and after the productization prerequisites in §58 are met. v0 remains local-only: the runtime and CLI must be usable without any cluster, git host, or hosted service.

5. Recommended implementation stack

Use TypeScript for the CLI, compiler, resolver, and runtime.

Use:

* Node.js 22 or later;
* JSON Schema Draft 2020-12;
* YAML for user-facing artifacts;
* SQLite for the local index and execution history;
* OCI containers or a restricted child process for function nodes;
* JSON Lines for execution event logs;
* SHA-256 content digests;
* Git-friendly directories for local artifacts.

Suggested repository structure:

mechanize/
  packages/
    cli/
    compiler/
    schema/
    runtime/
    registry-client/
    skill/
    test-fixtures/
  schemas/
    sentinel.schema.json
    variation.schema.json
    execution-event.schema.json
    tool-capability.schema.json
  examples/
  experiments/

6. User-facing commands

Implement these commands:

mechanize capture
mechanize compile
mechanize find
mechanize vary
mechanize resolve
mechanize validate
mechanize run
mechanize replay
mechanize inspect
mechanize publish
mechanize pull
mechanize experiment report

Additional commands, available in v1 (see §53–§58):

mechanize repo init          # bootstrap a sentinels-repo layout
mechanize scaffold ci        # generate CI/CD workflows into a sentinels repo
mechanize provision          # apply a Sentinel as a CR to a cluster
mechanize schedule           # create or update a SentinelSchedule CR
mechanize apply              # kubectl-style apply of a materialized CR manifest

Agent integrations may expose these through:

/mechanize
/mechanize run
/mechanize variations
/mechanize publish
/mechanize schedule          # v1
/mechanize provision         # v1

The CLI must remain usable independently of Claude, Codex, Cursor, Gemini, or Cardinal.

7. Local artifact layout

Use this local directory:

.mechanize/
  sentinels/
    <sentinel-name>/
      sentinel.yaml
      README.md
      fixtures/
      functions/
      tests/
  variations/
    <variation-name>.yaml
  runs/
    <run-id>/
      resolved-dag.yaml
      events.jsonl
      outputs/
      evidence/
      summary.json
  captures/
    <capture-id>/
      session.jsonl
      tool-calls.jsonl
      files.json
      environment.json
  index.db
  config.yaml

Sentinels should be suitable for committing into a repository.

Variations are local by default and should be added to .gitignore unless explicitly promoted or published.

8. Sentinel schema

The canonical user-facing Sentinel format is YAML.

Example:

apiVersion: mechanize.dev/v1alpha1
kind: Sentinel
metadata:
  name: post-deployment-error-regression
  displayName: Post-deployment error regression
  version: 1.0.0
  digest: sha256:GENERATED_AFTER_NORMALIZATION
  createdAt: 2026-08-01T20:00:00Z
  createdBy:
    type: agent-session
    captureId: cap_01K...
  labels:
    domain: observability
    procedure: baseline-comparison
spec:
  purpose:
    summary: >
      Determine whether a deployment caused a material increase in
      application errors relative to its recent baseline.
    conclusionType: regression-assessment
    reusableQuestion: >
      Did a recent change produce a statistically and operationally
      meaningful increase in errors?
  inputs:
    service:
      type: string
      required: true
    environment:
      type: string
      default: production
    deploymentWindow:
      type: duration
      default: 30m
    baselineWindow:
      type: duration
      default: 24h
    minimumErrorIncrease:
      type: number
      default: 0.25
      constraints:
        minimum: 0
  capabilities:
    required:
      - id: deployments.list
        capabilityType: tool
        inputSchemaDigest: sha256:...
        outputSchemaDigest: sha256:...
      - id: telemetry.query-timeseries
        capabilityType: tool
        inputSchemaDigest: sha256:...
        outputSchemaDigest: sha256:...
  variationPoints:
    - path: /spec/inputs/service
      operations: [bind]
    - path: /spec/inputs/environment
      operations: [bind]
    - path: /spec/inputs/minimumErrorIncrease/default
      operations: [replace]
    - path: /spec/nodes/query-current/config/toolRef
      operations: [replace-binding]
    - path: /spec/nodes/assess-causality/config/modelPolicy
      operations: [replace]
    - extensionPoint: before-final-condition
      operations: [insert-node]
  nodes:
    get-deployment:
      kind: tool
      dependsOn: []
      config:
        toolRef: deployments.list
        arguments:
          service: "${inputs.service}"
          environment: "${inputs.environment}"
          start: "${execution.now - inputs.deploymentWindow}"
          end: "${execution.now}"
        timeout: 30s
        retry:
          maxAttempts: 2
          backoff: 2s
      output:
        schema:
          type: object
          required: [deploymentId, deployedAt, revision]
    query-baseline:
      kind: tool
      dependsOn: [get-deployment]
      config:
        toolRef: telemetry.query-timeseries
        arguments:
          metric: application.errors
          service: "${inputs.service}"
          environment: "${inputs.environment}"
          start: "${nodes.get-deployment.output.deployedAt - inputs.baselineWindow}"
          end: "${nodes.get-deployment.output.deployedAt}"
          aggregation: rate
        timeout: 60s
        retry:
          maxAttempts: 3
          backoff: exponential
      output:
        schemaRef: schemas/timeseries.json
    query-current:
      kind: tool
      dependsOn: [get-deployment]
      config:
        toolRef: telemetry.query-timeseries
        arguments:
          metric: application.errors
          service: "${inputs.service}"
          environment: "${inputs.environment}"
          start: "${nodes.get-deployment.output.deployedAt}"
          end: "${execution.now}"
          aggregation: rate
        timeout: 60s
      output:
        schemaRef: schemas/timeseries.json
    compare-rates:
      kind: function
      dependsOn: [query-baseline, query-current]
      config:
        runtime: nodejs22
        source: functions/compare-rates.mjs
        sourceDigest: sha256:...
        entrypoint: compareRates
        network: disabled
        filesystem: readonly
        timeout: 10s
        arguments:
          baseline: "${nodes.query-baseline.output}"
          current: "${nodes.query-current.output}"
      output:
        schema:
          type: object
          required:
            - baselineRate
            - currentRate
            - relativeIncrease
            - sampleSufficient
    assess-causality:
      kind: llm
      dependsOn:
        - get-deployment
        - query-baseline
        - query-current
        - compare-rates
      config:
        task: >
          Determine whether the observed change is temporally and
          behaviorally consistent with the deployment causing the
          error-rate increase. Use only the supplied evidence.
        modelPolicy: analytical-small
        maxInputTokens: 12000
        maxOutputTokens: 1000
        temperature: 0
        evidence:
          deployment: "${nodes.get-deployment.output}"
          comparison: "${nodes.compare-rates.output}"
          baseline: "${nodes.query-baseline.output.summary}"
          current: "${nodes.query-current.output.summary}"
        outputSchema:
          type: object
          required:
            - classification
            - confidence
            - reasons
          properties:
            classification:
              enum:
                - consistent
                - inconsistent
                - inconclusive
            confidence:
              type: number
              minimum: 0
              maximum: 1
            reasons:
              type: array
              items:
                type: string
    regression-condition:
      kind: condition
      dependsOn:
        - compare-rates
        - assess-causality
      config:
        expression: >
          nodes.compare-rates.output.sampleSufficient == true &&
          nodes.compare-rates.output.relativeIncrease >=
            inputs.minimumErrorIncrease &&
          nodes.assess-causality.output.classification == "consistent"
      output:
        schema:
          type: boolean
    emit-finding:
      kind: emit
      dependsOn:
        - get-deployment
        - compare-rates
        - assess-causality
        - regression-condition
      when: "${nodes.regression-condition.output == true}"
      config:
        finding:
          type: deployment-error-regression
          title: >
            Error regression after deployment for ${inputs.service}
          severityExpression: >
            nodes.compare-rates.output.relativeIncrease >= 1.0
              ? "critical"
              : "warning"
          dedupeKey: >
            ${inputs.service}:${nodes.get-deployment.output.deploymentId}
          evidence:
            - "${nodes.get-deployment.output}"
            - "${nodes.compare-rates.output}"
            - "${nodes.assess-causality.output}"
  outputs:
    finding:
      value: "${nodes.emit-finding.output}"
      required: false
    assessment:
      value:
        deployment: "${nodes.get-deployment.output}"
        comparison: "${nodes.compare-rates.output}"
        causality: "${nodes.assess-causality.output}"
        triggered: "${nodes.regression-condition.output}"
  execution:
    concurrency: 1
    failureMode: fail-fast
    defaultTimeout: 5m
    maxCost:
      llmTokens: 20000
      toolCalls: 20
    deterministicReplay:
      supported: true
  provenance:
    captureId: cap_01K...
    retainedSteps:
      - tool-call-4
      - tool-call-7
      - local-script-2
      - reasoning-step-9
    omittedSteps:
      - id: tool-call-1
        reason: malformed-query
      - id: tool-call-2
        reason: rejected-hypothesis

9. Node model

Every node must have:

interface BaseNode {
  kind: "tool" | "function" | "llm" | "condition" | "emit" | "ask_human";
  dependsOn: string[];
  when?: Expression;
  config: unknown;
  output: OutputContract;
  cache?: CachePolicy;
}

Node kinds fall into three categories:

* infrastructural — tool, condition, emit;
* analytical — function, llm, ask_human. Selection between the three is governed by §32.

Node IDs must be stable and semantic.

Good:

query-baseline
compare-rates
assess-causality

Bad:

node-1
step-7
generated-task-a

Stable node identity is required because Variations patch nodes by path.

10. Tool-node contract

A tool node calls a mechanically invokable capability.

The runtime must maintain a capability registry:

interface ToolCapability {
  id: string;
  version?: string;
  description: string;
  inputSchema: JsonSchema;
  outputSchema: JsonSchema;
  invoke(input: unknown, context: ToolContext): Promise<unknown>;
  authRequirements?: AuthRequirement[];
  executionLocation: "local" | "remote" | "either";
}

Tool references must not depend on an MCP server’s display name alone.

Resolve tools using:

1. exact capability ID;
2. compatible input and output schema;
3. declared version constraint;
4. configured local binding.

Example local binding:

bindings:
  telemetry.query-timeseries:
    provider: cardinal-mcp
    tool: query_metrics
    version: ">=1.4 <2.0"

A Sentinel is portable only when its abstract capability can be rebound to a compatible provider.

11. Function-node contract

Function nodes represent deterministic transformations that do not already exist as tools.

Generated functions must:

* accept one JSON input object;
* return one JSON-compatible output object;
* have no undeclared inputs;
* have no implicit access to session context;
* have dependencies pinned;
* pass generated tests;
* run in a sandbox;
* declare network and filesystem permissions;
* be content-addressed.

Function signature:

export async function run(input: unknown): Promise<unknown>

The compiler must generate:

functions/<node-id>.mjs
tests/<node-id>.test.mjs
fixtures/<node-id>/*.json

Function nodes may not silently execute arbitrary shell commands.

Shell access, if added later, must be a separate node kind with explicit permissions.

12. LLM-node contract

LLM nodes represent analytical work that cannot honestly be reduced to deterministic code.

An LLM node must:

* have a single explicit task;
* receive bounded, declared evidence;
* produce schema-valid structured output;
* support an inconclusive result;
* specify a model policy rather than a hardcoded vendor model;
* expose token limits;
* prohibit undisclosed tool calls;
* record prompt, model, response, and schema-validation result.

LLM nodes may not access the original conversation during Sentinel execution.

All required context must be included in declared node inputs.

The runtime must reject an LLM node whose evidence exceeds its input budget unless a declared compression node or truncation policy exists.

13. Condition-node contract

Condition nodes must use a restricted deterministic expression language.

Do not use JavaScript eval.

Support:

* boolean operators;
* numeric comparisons;
* string equality;
* null checks;
* array length;
* references to inputs and upstream outputs;
* limited pure functions such as abs, min, max, and contains.

Conditions may not perform I/O.

14. Emit-node contract

Emit nodes produce findings or other final side effects.

For v0, support local emission to:

* stdout;
* JSON file;
* webhook through a configured tool binding.

An emitted finding must contain:

interface Finding {
  type: string;
  title: string;
  severity: "info" | "warning" | "critical";
  dedupeKey: string;
  observedAt: string;
  evidence: EvidenceReference[];
  attributes?: Record<string, unknown>;
}

The runtime must deduplicate findings using:

sentinel digest + variation digest + dedupeKey

14a. Ask-human-node contract

Ask-human nodes represent judgment that must be ratified by an operator at runtime, distinct from LLM autonomous judgment. They exist because some non-mechanizable judgments should not be delegated: high-stakes decisions, ambiguity the LLM cannot resolve from the declared evidence, decisions requiring accountability, and steps where the source investigation had a human-in-the-loop moment that a re-run must reproduce.

An ask_human node must:

* have a single explicit question;
* receive bounded, declared evidence for the operator to consider;
* declare an answer schema (typed contract for the operator's response);
* support an inconclusive or skip result;
* persist question, evidence-snapshot, answer, and answering-operator identity into the execution record;
* declare a timeout policy — either block-until-answered, or fall through to a declared default after a bounded wait.

Example config:

nodes:
  confirm-target-environment:
    kind: ask_human
    dependsOn: [discover-candidate-environments]
    config:
      question: >
        Which of these candidate environments should the investigation
        target? The source investigation ran against prod; a re-run may
        need a different choice.
      evidence:
        candidates: "${nodes.discover-candidate-environments.output}"
      answerSchema:
        type: object
        required: [environment]
        properties:
          environment: { type: string }
          skipInvestigation: { type: boolean }
      timeout:
        mode: block-until-answered
        maxWait: 24h
      justification: >
        The source investigation operator made this call by hand after
        a course-correction; automating it would silently pick the
        wrong environment when the answer is context-dependent.

Ask-human nodes may not access the original conversation. All context the operator sees at runtime must be included in declared node inputs. The runtime is responsible for surfacing the question through whatever operator channel is configured (CLI prompt, ticket, chat, email).

**Response normalization.** The node's declared `answerSchema` is a contract on the node's OUTPUT — not on the raw operator response. Operator channels differ in fidelity: a structured CLI form or a ticket with typed fields returns a schema-shaped object directly; a Slack reply, an email, or a free-form CLI prompt returns prose. To bridge this, the runtime MAY invoke an internal LLM parser to normalize free-form channel replies into the declared `answerSchema`, subject to the same inconclusive/skip discipline (a parse that cannot honestly produce a schema-conforming value must resolve as `inconclusive`, not as a guessed match). The parser is a runtime implementation detail, not a DAG node — the compiler does NOT emit a separate parser node after `ask_human`, and downstream nodes see only the schema-conforming output. The runtime MUST persist the raw operator response alongside the normalized answer in the execution record so a reviewer can audit any parse decision.

An ask_human node suspends downstream execution for its subgraph until answered (or the timeout policy fires). Other independent branches continue per the DAG's concurrency policy.

Findings emitted downstream of an ask_human node must record the answer in their evidence chain.

Ask-human nodes are subject to the "no optional analytical nodes" rule in §32.

15. Edge semantics

Edges are derived from dependsOn.

A Sentinel must be a directed acyclic graph.

Validation must reject:

* cycles;
* missing dependencies;
* references to downstream nodes;
* references to optional node outputs without null handling;
* multiple writers to the same output;
* undeclared external values.

A node becomes runnable when:

1. every dependency is terminal;
2. every required dependency succeeded;
3. its when expression evaluates to true or is absent;
4. concurrency capacity is available;
5. execution budget remains.

16. Node execution states

Every node transitions through:

PENDING
READY
RUNNING
SUCCEEDED
FAILED
SKIPPED
CANCELLED
CACHED

Allowed transitions:

PENDING -> READY
PENDING -> SKIPPED
PENDING -> CANCELLED
READY -> RUNNING
READY -> CANCELLED
RUNNING -> SUCCEEDED
RUNNING -> FAILED
RUNNING -> CANCELLED
READY -> CACHED

Terminal states:

SUCCEEDED
FAILED
SKIPPED
CANCELLED
CACHED

Each transition must generate an execution event.

17. DAG execution semantics

Execution begins with:

interface ExecutionContext {
  runId: string;
  sentinelDigest: string;
  variationDigest?: string;
  startedAt: string;
  now: string;
  inputs: Record<string, unknown>;
  bindings: CapabilityBindings;
  budgets: ExecutionBudgets;
}

Execution algorithm:

1. Load Sentinel.
2. Verify Sentinel digest.
3. Load Variation if provided.
4. Verify Variation references the exact base digest.
5. Apply Variation patches.
6. Validate the resolved DAG.
7. Resolve capability bindings.
8. Validate all runtime inputs.
9. Create execution record.
10. Identify root nodes.
11. Execute runnable nodes respecting concurrency.
12. Validate each node output against its schema.
13. Persist output before releasing dependent nodes.
14. Stop or continue based on failure policy.
15. Resolve declared Sentinel outputs.
16. Write execution summary.
17. Return process exit status.

Exit codes:

0  execution completed successfully
2  validation failed
3  capability binding missing
4  node execution failed
5  budget exceeded
6  resolved output invalid
7  internal runtime failure

18. Failure semantics

Support these Sentinel-level failure modes:

fail-fast

Cancel all not-yet-running nodes after the first required-node failure.

continue-independent

Continue branches that do not depend on the failed node.

Use fail-fast by default in v0.

Node retries must be declared explicitly.

Retry only:

* tool timeouts;
* retryable provider errors;
* process startup failures;
* LLM transport failures.

Do not automatically retry:

* schema-invalid output;
* deterministic function exceptions caused by input;
* failed conditions;
* unsupported capability bindings.

19. Caching semantics

Cache keys must include:

node kind
node configuration digest
resolved arguments digest
upstream output digests
tool or function version
Sentinel digest
Variation-affecting patch digest

LLM nodes must be uncached by default.

Tool and function nodes may be cached if declared.

A replay execution must be able to replace external tool calls with captured outputs.

20. Deterministic replay

Every successful run must generate a replay bundle:

runs/<run-id>/
  replay/
    manifest.yaml
    node-inputs/
    node-outputs/
    tool-responses/
    llm-responses/
    function-artifacts/

mechanize replay <run-id> must execute the DAG using captured external outputs.

Replay proves:

* the graph is structurally executable;
* functions still execute;
* expressions still resolve;
* outputs still validate;
* orchestration does not depend on the original session.

Replay does not prove that external systems still return equivalent data.

21. Variation schema

Example:

apiVersion: mechanize.dev/v1alpha1
kind: Variation
metadata:
  name: checkout-latency-regression
  createdAt: 2026-08-02T01:00:00Z
  locality: local
  labels:
    team: checkout
spec:
  base:
    name: post-deployment-error-regression
    version: 1.0.0
    digest: sha256:BASE_SENTINEL_DIGEST
  intent:
    originalQuestion: >
      Did the latest checkout deployment cause a latency regression?
    relationship: same-procedure-different-signal
    explanation: >
      Reuses deployment lookup, baseline/current window construction,
      comparison, causal assessment, and emission. Replaces the measured
      signal and comparison threshold.
  bindings:
    inputs:
      service: checkout-api
      environment: production
      minimumLatencyIncrease: 0.20
  patches:
    - op: replace
      path: /spec/nodes/query-baseline/config/arguments/metric
      value: http.server.duration.p95
    - op: replace
      path: /spec/nodes/query-current/config/arguments/metric
      value: http.server.duration.p95
    - op: replace
      path: /spec/nodes/compare-rates/config/source
      value: functions/compare-latency.mjs
    - op: replace
      path: /spec/nodes/compare-rates/config/sourceDigest
      value: sha256:...
    - op: replace
      path: /spec/nodes/regression-condition/config/expression
      value: >
        nodes.compare-rates.output.sampleSufficient == true &&
        nodes.compare-rates.output.relativeIncrease >=
          inputs.minimumLatencyIncrease &&
        nodes.assess-causality.output.classification == "consistent"
    - op: replace
      path: /spec/nodes/emit-finding/config/finding/type
      value: deployment-latency-regression
  addedInputs:
    minimumLatencyIncrease:
      type: number
      default: 0.20
      constraints:
        minimum: 0
  evidence:
    sourceCaptureId: cap_01K...
    matchedSentinelScore: 0.87
    unchangedNodeRatio: 0.71
    changedNodes:
      - query-baseline
      - query-current
      - compare-rates
      - regression-condition
      - emit-finding

22. Variation application semantics

Use an intentionally restricted subset of JSON Patch.

Supported operations:

bind
replace
replace-binding
disable
insert-before
insert-after

Do not support arbitrary remove in v0.

Variation resolution order:

1. Load immutable base Sentinel.
2. Verify digest.
3. Apply added inputs.
4. Apply input bindings.
5. Apply tool-binding replacements.
6. Apply scalar replacements.
7. Apply node replacements.
8. Apply insertions at declared extension points.
9. Apply optional-node disables.
10. Recompute dependencies.
11. Validate resolved graph.
12. Compute resolved graph digest.

A Variation must fail resolution when:

* its base digest is unavailable;
* the base digest differs;
* a patch targets a non-variation point;
* a replaced node violates the original output contract;
* an insertion creates a cycle;
* a required output becomes unreachable;
* a patch modifies provenance or digest fields.

23. Safe and unsafe Variations

Support two modes.

Safe Variation

May modify only declared variation points.

This is the default and the only mode counted as successful reuse evidence.

Unsafe fork

May modify arbitrary graph structure.

An unsafe fork must be materialized as a new Sentinel candidate.

Do not call an arbitrary graph rewrite a Variation.

This distinction prevents inflated reuse metrics.

24. Sentinel matching

When /mechanize is invoked, search for reusable Sentinels before generating a new one.

Matching must use both semantic and structural evidence.

Store this index per Sentinel:

interface SentinelIndexRecord {
  sentinelDigest: string;
  purposeEmbedding: number[];
  reusableQuestionEmbedding: number[];
  inputConcepts: string[];
  capabilityIds: string[];
  nodeKindSequence: string[];
  graphFingerprint: string;
  outputConcepts: string[];
  variationPointConcepts: string[];
}

Candidate retrieval:

1. semantic similarity of investigation objective;
2. overlap of required capability classes;
3. overlap of evidence types;
4. similarity of conclusion type;
5. compatibility of available tools;
6. compatibility of input concepts.

Do not match primarily on domain words.

For example, these may use the same procedure despite different domains:

* “Did the deployment increase error rate?”
* “Did the campaign increase checkout abandonment?”
* “Did the configuration rollout increase queue depth?”

The shared procedure is:

identify change event
construct baseline window
construct current window
query signal
normalize signal
compare windows
assess causal consistency
emit finding

25. Procedure signature

The compiler must generate a normalized procedure signature:

procedureSignature:
  objectiveClass: change-impact-assessment
  evidencePattern:
    - change-event
    - baseline-window
    - comparison-window
    - measured-signal
  transformations:
    - normalize
    - compare
  judgments:
    - causal-consistency
  outputClass: regression-finding

Use the procedure signature for retrieval and reuse decisions.

The signature must describe the investigation logic rather than the implementation vendor.

Bad signature:

query Datadog and inspect Kubernetes

Good signature:

compare a post-change signal against its pre-change baseline

26. Variation decision

For each candidate Sentinel, the compiler must produce:

interface ReuseAssessment {
  candidateDigest: string;
  reusable: boolean;
  confidence: number;
  preservedProcedureSteps: string[];
  requiredChanges: ProposedPatch[];
  incompatibleSteps: string[];
  unchangedNodeRatioEstimate: number;
  rationale: string;
}

Create a Variation only when:

* the investigation objective maps to the same procedure signature;
* the base outputs remain semantically meaningful;
* required patches target safe variation points;
* no more than 40% of nodes require replacement;
* at least one nontrivial evidence-gathering or analytical node is preserved;
* the resolved DAG passes validation and execution.

The 40% threshold is an experimental starting point, not a permanent product rule.

27. Structural reuse metrics

Compute these metrics for every Variation:

Node reuse ratio

unchanged base nodes / total base nodes

Edge reuse ratio

unchanged base edges / total base edges

Procedure-step reuse ratio

preserved normalized procedure steps /
total base procedure steps

Artifact reuse ratio

Weighted score:

0.4 × node reuse ratio
+ 0.2 × edge reuse ratio
+ 0.3 × procedure-step reuse ratio
+ 0.1 × function artifact reuse ratio

Patch size

Record:

* number of patch operations;
* bytes in Variation;
* bytes in base Sentinel;
* Variation/base size ratio.

A compelling Variation should usually be materially smaller than the base Sentinel.

28. Investigation capture

The compiler should not depend on raw conversation text alone.

Capture:

session messages
tool calls
tool arguments
tool outputs or output digests
generated files
executed commands
command outputs
environment metadata
agent conclusions
user corrections

Canonical capture event:

interface CaptureEvent {
  id: string;
  timestamp: string;
  type:
    | "user-message"
    | "assistant-message"
    | "tool-call"
    | "tool-result"
    | "file-write"
    | "command"
    | "command-result"
    | "decision"
    | "conclusion"
    | "attachment";
  parentId?: string;
  attachmentRefs?: string[];   // IDs of attachment events this event references
  payload: unknown;
  redactions?: RedactionRecord[];
}

Attachments are non-text payloads (images, PDFs, audio, video, arbitrary binaries) that appear in a session. They are surfaced as their own event type so the compiler cannot mistake them for text evidence.

interface AttachmentEvent extends CaptureEvent {
  type: "attachment";
  payload: {
    kind:
      | "image"
      | "pdf"
      | "audio"
      | "video"
      | "binary"
      | "unknown";
    mimeType?: string;
    sizeBytes: number;
    contentDigest: string;      // SHA-256 of the raw payload
    sourceRef: {
      kind: "transcript-inline" | "file-path" | "url-reference";
      location?: string;         // path or offset; never the raw content
    };
    captionHint?: string;        // caption or alt text from the source, if any
  };
}

A message event or tool-result event that visually or structurally referenced an attachment records the attachment's event ID in attachmentRefs. The harvester never inlines the raw payload into another event.

The capture adapter may vary by agent.

Normalize all adapters into this event format.

28.1 Transcript adapter contract

An agent's on-disk session format is not a public contract. Formats can change without notice, and each agent (Claude Code, Codex, Cursor, Gemini) writes something different on disk. The harvester layer isolates this brittleness.

Rules:

* One adapter module per agent, owned in the same repository as that agent's cardinal-agent-plugins hooks.
* Every adapter is a pure function: raw session file(s) plus a starting cursor produce a stream of CaptureEvent values (§28 schema) and an updated cursor.
* Every adapter must:
  - detect the format version it is parsing, either via an explicit header/marker or via a version probe;
  - refuse to parse an unrecognized version rather than silently emit wrong events;
  - assign a stable, content-derived event ID (SHA-256 of the canonical event payload, prefixed by the event's ordinal within the session) so downstream references such as §47 audit-log citations survive re-harvest;
  - preserve the parent-child relationship between tool-call and tool-result events using the agent's native identifiers where available;
  - mark any event whose payload was truncated or elided by the agent's own compaction as truncated: true.
* Adapter versioning is independent of the mechanize package version. An adapter can ship a fix without a runtime release.
* Adapters must run read-only. They may not modify the source transcript in any way, including for reformatting or "repair."
* Adapters must recognize non-text payloads (images, PDFs, audio, video, binaries) and emit them as attachment events (§28). Non-text payloads must never be decoded, base64-inlined, transcribed to text, resized, transcoded, or described. The adapter records only kind, mime type, size, SHA-256 digest, source reference, and any caption or alt text the transcript already carries.
* When a tool_use or tool_result contains mixed text and non-text content, the adapter splits it: text portions become the payload of the tool-call/tool-result event; each non-text portion becomes a separate attachment event, referenced by ID from the originating event's attachmentRefs.

If an adapter cannot parse a session, the harvester surfaces capture_status = unparseable with the detected format markers. Silent failure is strictly worse than no answer.

28.2 Redaction discipline

Raw on-disk transcripts contain unredacted values: secrets the model saw, environment-variable values, credentials pasted mid-session, PII in tool outputs. Redaction must be enforced at the boundary where the mechanize layer first surfaces a payload to any caller.

Rules:

* A single shared redaction module defines the rules. In this repository that module is cardinal_core.redaction.
* Both the plugin hooks (write-side telemetry) and the harvester (read-side capture) must use this module. No adapter or tool may implement its own redaction.
* The harvester tool API has no unredacted access path. There is no "raw" mode, no debug flag, no environment override that returns unredacted content.
* Redaction rules include, at minimum:
  - known secret patterns (API keys, JWTs, bearer tokens, private keys);
  - values matching the process's environment-variable values at capture time;
  - .env-like file contents;
  - any tool-result string above a configured size threshold, returned as a content digest plus the first and last N bytes only.
* Redacted output preserves structural shape. Keys are retained; values are replaced with typed placeholders such as <REDACTED:api-key>, so downstream compilation does not lose type or shape information.
* Every event exposes a redactions array listing what was elided (kind, byte range or JSON pointer, replacement placeholder), so the compiler can reason about coverage without seeing the values.
* When the redaction module cannot classify a payload, the default is to elide rather than pass through.
* Attachment payloads (images, PDFs, binaries) are not subjected to text-pattern redaction. They are preserved as attachment references (§28) with content digest and size only; the raw bytes are never surfaced through the harvester tool API. If an attachment's captionHint contains text, the caption is subject to normal text redaction.

The same discipline applies to fixture generation (§11, §37 evidence retention). A fixture derived from a captured tool output must pass through the same redaction pipeline before being written to disk.

28.3 Harvester tool surface

The compiler and the /mechanize skill do not read transcript files directly. They call a small set of tools that expose captures through the CaptureEvent schema. The surface is designed for the flow a mechanizing agent actually runs, not for arbitrary transcript inspection.

Design principles:

* Progressive disclosure. No tool returns the entire capture in one call. Summaries are cheap; full payloads are opt-in and pageable.
* Redacted by default, always. See §28.2.
* Stable, content-derived event IDs. Every event ID survives re-harvest and can be cited in the compiler audit log (§47).
* Capture digest on every response. Every tool response carries capture_id and capture_digest (SHA-256 of the raw source at read time). Digest drift between two calls signals the underlying session has been written since the first read.
* Two entry points: current session and past session. The agent knows which flow it is in; the surface reflects that.
* Fail loud, never silent. Unparseable, missing, or too-new transcripts produce a typed status, not an empty event list.

Tool surface:

capture_current() -> CaptureHandle

Return the capture handle for the session the caller is executing in, or a typed error explaining why one is not available (unsupported agent, no session detected, harness environment missing). The handle contains:

  capture_id
  capture_digest
  agent
  started_at
  last_event_at
  status                # in_progress | completed | unparseable
  summary               # a CaptureSummary (see capture_summary)

Cheap; safe to call at skill startup. When status is in_progress, subsequent calls may return a newer capture_digest.

capture_search(query, filters?) -> [CaptureCandidate]

Fuzzy-search past captures by objective, tool inventory, file paths touched, or free text. Optional filters: agent, cwd, since, until, min_tool_calls, has_tool_id. Each candidate is a shallow record:

  capture_id
  agent
  started_at
  ended_at
  objective_snippet     # redacted
  tool_id_set
  event_count
  match_reasons         # why this candidate ranked

Ranked. Redacted. Bounded result count. This is the primary entry point for compiling a past investigation.

capture_list(filters?, cursor?) -> Page[CaptureCandidate]

Enumerate captures without a query, filtered and paginated. Same result shape as capture_search. For the browsing case: "show me my last week of sessions on this repo."

capture_summary(capture_id) -> CaptureSummary

Return a compact view sufficient to decide "is this worth compiling?" without pulling the event stream:

  objective                 # first user message, redacted
  conclusion                # last assistant messages before session end,
                            # redacted; marked pending when status = in_progress
  event_counts_by_type
  tool_inventory            # deduped list of tool IDs with call counts and
                            # first/last-seen timestamps
  files_written             # paths only, redacted
  commands_executed         # command names and argv shape, redacted
  timing                    # started_at, ended_at, wall duration
  unparseable_regions       # ordinals or byte offsets, if any
  truncated_regions         # events flagged truncated: true

Deterministic; cacheable by capture_digest.

capture_events(capture_id, filters?, cursor?, limit?) -> Page[CaptureEvent]

Return the CaptureEvent stream in order. Filters map to what the compiler asks about, not to arbitrary transcript queries:

  types            # subset of the CaptureEvent type enum
  tool_id          # filter to calls of a specific tool
  after_event_id   # slice
  before_event_id  # slice
  has_error        # retained-and-failed calls, per §29 stage 3
  parent_id        # fetch children of a specific tool-call

Payload fields inside each event are redacted per §28.2 and carry a redactions metadata array. Each event has its stable id.

capture_event(capture_id, event_id) -> CaptureEvent

Return one event's full (redacted) payload by ID. This is the deep-link tool: when the compiler audit log records captureEvent: tool-call-7 → decision: retained, a reviewer or a follow-up agent uses this to see exactly what tool-call-7 was.

capture_payload_digest(capture_id, event_id) -> PayloadDigest

Return the SHA-256 of the raw payload before redaction, plus size in bytes and MIME/type hint if known. Used by the compiler to decide whether a captured tool output is worth retaining verbatim in fixtures without transferring the payload itself. Never returns content.

Intended flows:

Compile current session:

  1. capture_current -> handle + summary. Decide there is enough substance to compile.
  2. capture_events(id, types=[tool-call, tool-result, decision, conclusion]) -> walk the retained-event stream in the compiler.
  3. capture_event(id, event_id) on demand for any event the compiler needs to inspect closely.
  4. capture_payload_digest before deciding to retain a large tool output as a fixture.

Compile a past session:

  1. capture_search(query) -> ranked candidates with match_reasons.
  2. capture_summary(id) on the top one or two -> confirm the right session.
  3. Steps 2–4 above.

Non-goals of this surface:

* No "give me everything" endpoint. If it fits in one call, the session was too small to bother compiling.
* No mutation endpoints. Captures are read-only from the compiler's perspective.
* No agent-scoped raw-file access. If a future compiler stage needs something the tools do not expose, the correct fix is to extend the tool surface, not to bypass it.
* No attachment content in any response. Attachment events surface kind, mime type, size, digest, source reference, and caption hint only. No tool decodes, resizes, transcodes, or transcribes attachment bytes. If a future compiler stage needs multimodal reasoning, the correct fix is a separate typed tool (`capture_attachment_bytes`) with explicit consent, not a hidden decode path in this surface.

The full surface should be spellable in one page of documentation. If it grows, that is a signal that a proposed extension is really general-purpose transcript tooling and belongs elsewhere.

29. Compiler stages

Implement the compiler as explicit stages.

Stage 1: capture normalization

Convert agent-specific history into canonical capture events.

Stage 2: investigation segmentation

Identify:

* investigation start;
* objective;
* hypotheses;
* evidence-gathering actions;
* analytical steps;
* conclusion;
* irrelevant conversation.

Stage 3: causal contribution analysis

For each action, classify:

REQUIRED
SUPPORTING
EXPLORATORY
FAILED
INCIDENTAL
LOCAL_ONLY

Retain REQUIRED.

Retain SUPPORTING only when needed for reproducibility or confidence.

Attachments referenced by a retained action are not text and must not be treated as text evidence. For any retained action whose input includes one or more attachment references, the compiler must choose exactly one of:

* emit the attachment as a Sentinel input of type image, pdf, or binary, so the runtime user must supply an equivalent artifact each execution;
* emit the human-derived inference from the attachment as a plain-typed Sentinel input (for example, a boolean "spike observed" or a structured "anomaly summary"), and record in the audit log that this input replaces a visual observation;
* mark the action requires-manual-input: true in the audit log and stop generation until a reviewer resolves it;
* omit the action and its downstream if none of the above is honest.

The compiler must never emit a text description of an attachment's content and treat that description as captured evidence. That is a hallucination cloaked as compilation. The audit log (§47) must record, for every attachment reference the compiler encountered, which of the four options was taken and why.

Stage 4: procedure extraction

Produce a vendor-independent procedure signature.

Stage 5: Sentinel search

Search local and optionally remote indexes.

Stage 6: reuse assessment

Determine whether to produce:

NEW_SENTINEL
SAFE_VARIATION
UNSAFE_FORK
UNSUPPORTED

Stage 7: graph synthesis

Map retained steps to node kinds.

Stage 8: generated implementation

Generate function code, schemas, fixtures, and tests.

Stage 9: graph validation

Validate schema, references, DAG structure, capabilities, and budgets.

Stage 10: trial execution

Execute against the current or equivalent fresh inputs.

Stage 11: conclusion comparison

Compare execution output with the original investigation’s conclusion.

Stage 12: artifact persistence

Persist only after successful execution or explicit user override.

30. Tool-call synthesis rules

A captured tool call becomes a tool node only when:

* the tool can be resolved later;
* arguments can be expressed using inputs or upstream outputs;
* outputs can be typed;
* credentials are not embedded;
* the call is not inherently one-off;
* the tool has an execution binding.

Literal one-time values should become parameters where appropriate.

Example:

Captured:

{
  "service": "checkout-api",
  "start": "2026-08-01T11:00:00Z",
  "end": "2026-08-01T11:30:00Z"
}

Compiled:

service: "${inputs.service}"
start: "${nodes.get-deployment.output.deployedAt}"
end: "${execution.now}"

The compiler must distinguish constants from accidental literals.

31. Generated-function synthesis rules

Generate a function node when the investigation included deterministic work such as:

* parsing;
* normalization;
* joins;
* aggregation;
* statistical comparison;
* filtering;
* threshold evaluation;
* data-shape conversion.

Do not create an LLM node for transformations that can be expressed deterministically.

Every generated function must have at least:

* one fixture from the original investigation;
* one edge-case fixture;
* one malformed-input test;
* schema validation.

A function node may not be synthesized from a step whose input includes an attachment payload. If deterministic transformation of image, PDF, or binary content is genuinely required, the compiler marks the step UNSUPPORTED (§29 stage 6). v0 has no multimodal function-node runtime and will not fake one.

32. Analytical-node selection rule

For every step in the source investigation that produces a value or judgment (as opposed to an external side effect), the compiler must choose exactly one of function, llm, or ask_human. The choice is a decision procedure, not a preference:

1. Can the judgment be expressed as a deterministic transformation over its declared inputs? → function.

2. Otherwise, is the judgment safe to delegate to an LLM without human ratification? A judgment is LLM-safe when both of the following hold:
   * it is qualitative in nature (semantic classification, causal interpretation across signals, qualitative evidence synthesis, ambiguous text interpretation, domain judgment that cannot be reduced to a deterministic rule);
   * its output can be acted on by downstream nodes without an operator confirming it first — that is, either downstream consumers are read-only, or the LLM's failure mode is bounded (a wrong classification produces a wrong finding, not a destructive action).
   If both hold → llm.

3. Otherwise (non-mechanizable AND requires human ratification before downstream nodes act) → ask_human.

Before creating an llm or ask_human node, the compiler must record:

judgmentJustification:
  kind: llm | ask_human
  deterministicAlternativeConsidered: true
  reasonRejected: >
    One-sentence explanation of why the deterministic option fails,
    grounded in the source investigation's actual reasoning.
  delegationSafetyConsidered:      # required when kind == llm
    autonomousDelegationAcceptable: true
    reason: >
      Why the LLM's judgment can be acted on without operator
      ratification.

This keeps the DAG as mechanical as possible while making the choice between LLM-autonomous and human-ratified explicit rather than implicit.

Non-existence of optional analytical nodes

There is no such thing as an optional llm or ask_human node. If a judgment is on the reusable procedure's critical path, its node is required. If a judgment is sometimes needed and sometimes not, the compiler must express that as one of:

* an operator-supplied input at Binding time (the input carries the judgment's output; the node is absent from that Binding's resolved DAG);
* a Variation that adds or removes the node from a base Sentinel.

The pattern "some input is optional → the analytical node that consumes it is optional" is prohibited. It hides whether the Sentinel's core procedure requires the judgment. A judgment either is part of the procedure (required node) or is not (input, or Variation).

Function nodes may be optional (gated by when: expressions on their inputs), because a function's presence-or-absence does not change what the DAG claims the procedure IS — only whether a particular execution needed that branch. LLM and ask_human nodes change the procedure claim and must not be gated by when: on their own kind.

33. Local-only dependencies

Every retained action must receive a portability status:

READY
PACKAGEABLE
REQUIRES_BINDING
LOCAL_ONLY
UNSUPPORTED

A Sentinel cannot be marked executable if a required node is LOCAL_ONLY.

The compiler must either:

* turn the dependency into an input;
* generate a packageable function;
* replace it with a capability requirement;
* omit the dependent branch;
* mark mechanization incomplete.

Do not conceal local-only dependencies inside prompts.

34. Publishing model

A user may publish:

* a Sentinel;
* a promoted Variation;
* metadata only;
* fixtures optionally;
* function artifacts optionally.

Publishing must be explicit.

Remote identifier:

mechanize://<publisher>/<name>@<version>

Registry object:

interface PublishedSentinel {
  manifest: Sentinel;
  digest: string;
  signature: string;
  artifacts: ArtifactReference[];
  visibility: "public" | "private";
  publishedAt: string;
}

The remote registry must be content-addressed.

An existing version may not be overwritten.

35. Variation publication

Variations remain local by default.

The user may:

Publish as Variation

The remote registry stores the overlay plus its base Sentinel reference.

Use when the base Sentinel is also remotely resolvable.

Promote to Sentinel

Materialize the resolved DAG and publish it as an independent Sentinel.

Use when:

* the Variation has become broadly useful;
* the base is private;
* the graph has diverged materially;
* the Variation introduces a new procedure signature.

36. Security requirements

Before persistence or publication:

* redact secrets from captures using the shared redaction module described in §28.2 — no adapter, harvester tool, or compiler stage may implement its own redaction path;
* replace credentials with binding references;
* reject environment variables whose values appear in artifacts;
* scan generated code for unsafe APIs;
* require explicit permissions for network access;
* store LLM prompts and outputs locally by default;
* never publish captured raw tool outputs automatically.

Generated functions must run with:

no network
read-only artifact directory
temporary writable directory
CPU limit
memory limit
wall-clock timeout
no inherited environment except allowlisted variables

37. Experiment 1: real investigation becomes a Sentinel

Select a real investigation with:

* at least three tool calls;
* at least one deterministic transformation;
* at least one analytical decision;
* a clear final conclusion;
* data that can change between executions.

Recommended initial investigation:

Determine whether a recent deployment caused an application error regression.

Required flow:

1. Perform the investigation interactively.
2. Capture the full session and tool trace.
3. Invoke /mechanize.
4. Generate a Sentinel.
5. Validate the Sentinel.
6. Execute the Sentinel against fresh data.
7. Replay the Sentinel from captured outputs.
8. Compare both outputs with the original conclusion.

Pass criteria:

* DAG has at least five nodes.
* At least two node kinds are present.
* The DAG executes without access to the original conversation.
* Every node output validates.
* Replay succeeds.
* The Sentinel conclusion is equivalent to the original conclusion.
* No required node is local-only.
* The full execution can be initiated through mechanize run.

Evidence to retain:

original capture
compiled Sentinel
compiler decision log
function source and tests
execution event log
resolved outputs
conclusion comparison
replay bundle

38. Experiment 2: nearby investigation becomes a Variation

Perform a second investigation that is different but procedurally related.

Preferred sequence:

Investigation A

Did the deployment increase application errors?

Investigation B

Did the deployment increase p95 latency?

These differ in:

* measured signal;
* transformation details;
* threshold;
* output type.

They share:

* deployment identification;
* baseline construction;
* current-window construction;
* data query pattern;
* comparison procedure;
* causal assessment;
* finding emission.

Required flow:

1. Complete Investigation B interactively.
2. Invoke /mechanize.
3. Search existing Sentinels.
4. Rank Investigation A’s Sentinel as a reuse candidate.
5. Generate a reuse assessment.
6. Produce a safe Variation.
7. Resolve the Variation.
8. Execute the resolved DAG.
9. Compare its conclusion to Investigation B.
10. Record structural reuse metrics.

Pass criteria:

* The system selects the existing Sentinel without being explicitly told its name.
* The result is a safe Variation, not a new Sentinel.
* At least 60% of nodes remain unchanged.
* At least 70% of edges remain unchanged.
* The Variation is less than 40% of the base Sentinel size.
* The resolved DAG executes successfully.
* The result matches the interactive investigation’s conclusion.
* No unchanged node is semantically invalid for the second investigation.
* The base Sentinel remains byte-identical.

39. Experiment 3: cross-service parameter variation

Use the same procedure and signal for a different service.

Example:

Base Sentinel:
checkout-api deployment error regression
Variation:
payments-api deployment error regression

Expected change:

* input binding only;
* possibly tool-provider binding;
* no node replacement.

Pass criteria:

* 100% node reuse;
* 100% edge reuse;
* Variation contains only bindings;
* execution succeeds.

This experiment proves simple parameterization but is not sufficient by itself to validate the product thesis.

40. Experiment 4: negative reuse case

Perform a superficially similar investigation whose procedure is different.

Example:

Why is checkout latency currently high?

This may require:

* dependency decomposition;
* trace exploration;
* bottleneck localization;
* no deployment event;
* no baseline/current comparison.

The compiler must not force this into the deployment-regression Sentinel.

Pass criteria:

* reuse assessment rejects the candidate;
* rationale identifies procedure mismatch;
* system creates a new Sentinel candidate or marks unsupported;
* no misleading high reuse score is reported.

This prevents the system from manufacturing variation success.

41. Experiment 5: adaptation through node insertion

Start with a base Sentinel that compares pre-change and post-change signal values.

Second investigation requires excluding known maintenance windows before comparison.

The Variation should:

* insert a deterministic filtering node;
* redirect one edge through the inserted node;
* preserve the rest of the DAG.

Pass criteria:

* insertion occurs only at a declared extension point;
* no cycle is introduced;
* base node output contracts remain unchanged;
* resolved execution succeeds;
* at least 70% of base nodes remain unchanged.

42. Experiment 6: alternate tool provider

Run the same Sentinel with two telemetry providers.

Example:

Provider A: Cardinal MCP
Provider B: Prometheus-compatible MCP

The abstract capability is:

telemetry.query-timeseries

Pass criteria:

* Sentinel graph remains unchanged;
* only capability bindings change;
* both outputs conform to the same schema;
* the comparison function receives provider-neutral input;
* both executions complete.

This proves that the DAG is not merely recorded vendor-specific tool calls.

43. Experiment report format

Generate one report per experiment:

experiment:
  id: exp-002
  hypothesis: >
    A latency regression investigation can reuse the error-regression
    Sentinel through a safe Variation.
  baseSentinel: sha256:...
  variation: sha256:...
  sourceCapture: cap_...
  outcome: pass
metrics:
  nodeReuseRatio: 0.714
  edgeReuseRatio: 0.80
  procedureStepReuseRatio: 0.875
  artifactReuseRatio: 0.79
  patchOperationCount: 6
  baseBytes: 18420
  variationBytes: 3410
  variationSizeRatio: 0.185
execution:
  status: succeeded
  durationMs: 18431
  toolCalls: 3
  functionCalls: 1
  llmCalls: 1
  outputEquivalentToInteractiveInvestigation: true
review:
  invalidlyReusedNodes: []
  unnecessaryChangedNodes: []
  reviewerDecision: accepted

Also generate a Markdown summary for human review.

44. Human review protocol

For every Variation experiment, require a reviewer to answer:

1. Does the base and variation share the same investigation procedure?
2. Are the unchanged nodes genuinely reusable?
3. Did the compiler preserve any node that should have changed?
4. Did the compiler change any node unnecessarily?
5. Would the Variation be understandable without the original session?
6. Is the Variation materially easier to produce than a new DAG?
7. Would the reviewer reuse this artifact again?

Do not treat automated graph similarity as sufficient evidence.

45. Comparison baseline

For each second investigation, produce two artifacts:

Reuse path

Existing Sentinel plus generated Variation.

Fresh-generation path

Generate a new Sentinel while hiding the existing Sentinel.

Compare:

* compilation time;
* execution success;
* node count;
* duplicated function code;
* duplicated prompts;
* review burden;
* artifact size;
* conclusion correctness;
* human preference.

The reuse hypothesis becomes credible if the Variation path is:

* smaller;
* easier to review;
* equally or more correct;
* more consistent with the established procedure.

46. Required instrumentation

Emit compiler events:

capture.loaded
investigation.segmented
procedure.extracted
sentinel.search.started
sentinel.candidate.scored
reuse.assessed
graph.synthesized
function.generated
variation.generated
graph.validated
execution.started
node.started
node.completed
execution.completed
conclusion.compared
artifact.persisted
artifact.published

Each event must include:

timestamp
capture ID
Sentinel digest
Variation digest
run ID
stage duration
status
structured details

47. Compiler audit log

Persist an audit record showing why each investigation step became:

* a tool node;
* a function node;
* an LLM node;
* omitted;
* parameterized;
* represented as a Variation patch.

Example:

captureEvent: tool-call-7
decision: retained
node: query-current
nodeKind: tool
reason: >
  This call produced evidence directly used in the final comparison.
generalization:
  service:
    from: checkout-api
    to: "${inputs.service}"
  timeRange:
    from: literal
    to: derived-from-deployment

This is essential for debugging compiler quality.

48. API boundaries

Compiler:

compileInvestigation(
  capture: InvestigationCapture,
  options: CompileOptions
): Promise<CompilationResult>

Search:

findReusableSentinels(
  procedure: ProcedureSignature,
  context: CapabilityContext
): Promise<SentinelCandidate[]>

Variation synthesis:

createVariation(
  investigation: InvestigationModel,
  base: Sentinel,
  assessment: ReuseAssessment
): Promise<Variation>

Resolver:

resolveVariation(
  base: Sentinel,
  variation: Variation
): ResolvedDag

Runtime:

executeDag(
  dag: ResolvedDag,
  inputs: Record<string, unknown>,
  bindings: CapabilityBindings
): Promise<ExecutionResult>

Registry:

publishSentinel(
  sentinel: Sentinel,
  artifacts: Artifact[]
): Promise<PublishedReference>

49. Validation layers

Run validation in this order:

Schema validation

Artifact conforms to JSON Schema.

Referential validation

All references resolve.

Graph validation

DAG is acyclic and outputs are reachable.

Type validation

Expressions and node arguments match schemas.

Capability validation

Required tools have compatible bindings.

Portability validation

No required local-only dependencies remain.

Security validation

Generated code and artifacts satisfy policy.

Trial execution

Graph executes against fixtures or live data.

Conclusion validation

Result is compared with original investigation.

A Sentinel is not considered complete after schema validation alone.

50. Definition of done for v0

v0 is complete when the repository contains:

* canonical Sentinel schema;
* canonical Variation schema;
* local artifact store;
* SQLite index;
* agent capture adapter for at least one agent;
* compiler pipeline;
* local Sentinel search;
* safe Variation generation;
* Variation resolver;
* DAG validator;
* local executor;
* tool nodes;
* function nodes;
* LLM nodes;
* condition nodes;
* emit nodes;
* replay bundles;
* publishing to a minimal remote registry;
* experiment runner;
* experiment reports;
* at least one passing real Sentinel experiment;
* at least two passing Variation experiments;
* at least one correct negative reuse experiment.

51. Evidence required before broadening the product

Do not add more node kinds or invest in hosted execution until the experiments answer these questions:

1. Does the compiler extract a procedure that users recognize as the real investigation?
2. Does the resulting Sentinel execute without session context?
3. Can users understand and review the generated DAG?
4. Does the second investigation retrieve the correct existing Sentinel?
5. Are Variations materially smaller than fresh Sentinels?
6. Do Variations preserve valid structure rather than superficial syntax?
7. Can the same Sentinel survive different tools through capability bindings?
8. Do users prefer editing a Variation to regenerating a workflow?
9. Do repeated Variations reveal stable variation points that can improve the base Sentinel?
10. Do users choose to publish any Sentinel without being prompted?

52. Most important design constraint

Do not optimize the compiler to maximize reuse percentage.

Optimize it to preserve the largest semantically valid investigation procedure.

A high node-reuse score is harmful if the reused nodes no longer represent the second investigation.

The product succeeds when the user sees the second result and says:

This is the same investigation, adapted to a new case.

Not:

The system managed to reuse some YAML.

The strongest implementation choice here is making Variations restricted overlays over declared variation points. Without that restriction, the system can claim every newly generated graph is a “variation,” and the experiment proves nothing.

53. Kubernetes CRDs

The v1 execution surface is Kubernetes. Sentinels, Variations, capability bindings, schedules, and runs are Custom Resources under the mechanize.dev API group. This section defines the CRD shape and the controller's contract. OpenAPI schemas are derived from §8, §21, and this section — this document is the source of truth.

API group:    mechanize.dev
Version:      v1alpha1

Kinds (all namespaced):

Sentinel
Variation
SentinelBinding
SentinelSchedule
SentinelRun

Design rules:

* Sentinel and Variation CRs carry the full authored spec inline so kubectl get sentinel -o yaml renders the actual procedure. Only large artifacts (function-node OCI images, fixtures, replay bundles) are referenced by digest and fetched at run time.
* Sentinel.spec and Variation.spec are immutable after admission. New content = new object. Identity uses the immutable digest as a required label; the admission webhook enforces "one digest, one object" per namespace.
* Findings emitted by a SentinelRun are surfaced through SentinelRun.status.findings. Downstream routing to paging, ticketing, or messaging is a Binding concern, not a controller concern.
* All resources support ownerReferences: SentinelSchedule owns SentinelRun; SentinelRun owns its Pod.

Admission webhook enforces, at minimum:

* the digest label matches the canonical SHA-256 of the applied spec;
* redaction (§28.2) was applied — the manifest contains no known secret patterns and no values matching common credential shapes;
* every capability ID referenced by a Sentinel or Variation resolves to a SentinelBinding in the target namespace;
* Variation.spec.base resolves to a Sentinel present in-cluster or in a configured trust store;
* the manifest's authored fields conform to the Sentinel/Variation schema.

Controller reconciles:

* SentinelSchedule → creates SentinelRun objects on the cron schedule, honoring concurrencyPolicy and history limits;
* SentinelRun → creates a Pod running the executor image with the resolved DAG mounted, watches to completion, and populates status with the node timeline, exit code, budgets consumed, and findings.

Minimal Sentinel CR:

apiVersion: mechanize.dev/v1alpha1
kind: Sentinel
metadata:
  name: post-deployment-error-regression-1-0-0
  namespace: observability
  labels:
    mechanize.dev/name: post-deployment-error-regression
    mechanize.dev/version: 1.0.0
    mechanize.dev/digest: sha256-abc123
spec:
  # verbatim Sentinel spec from §8

Minimal Variation CR:

apiVersion: mechanize.dev/v1alpha1
kind: Variation
metadata:
  name: checkout-latency-regression
  namespace: observability
  labels:
    mechanize.dev/base: post-deployment-error-regression-1-0-0
    mechanize.dev/digest: sha256-def456
spec:
  base:
    ref:
      name: post-deployment-error-regression-1-0-0
      digest: sha256:BASE_SENTINEL_DIGEST
  # verbatim Variation spec from §21

Minimal SentinelBinding CR:

apiVersion: mechanize.dev/v1alpha1
kind: SentinelBinding
metadata:
  name: default-telemetry
  namespace: observability
spec:
  bindings:
    telemetry.query-timeseries:
      provider: cardinal-mcp
      endpointRef:
        secretName: cardinal-mcp-endpoint
      version: ">=1.4 <2.0"
    deployments.list:
      provider: argocd-mcp
      version: ">=0.9"

Minimal SentinelSchedule CR:

apiVersion: mechanize.dev/v1alpha1
kind: SentinelSchedule
metadata:
  name: checkout-error-regression-hourly
  namespace: observability
spec:
  schedule: "*/15 * * * *"
  sentinelRef:
    name: post-deployment-error-regression-1-0-0
    digest: sha256:...
  variationRef:                    # optional
    name: checkout-latency-regression
  inputs:
    service: checkout-api
    environment: production
  suspend: false
  concurrencyPolicy: Forbid        # Allow | Forbid | Replace
  successfulRunHistoryLimit: 10
  failedRunHistoryLimit: 20
  budgets:
    llmTokens: 20000
    toolCalls: 20

Minimal SentinelRun CR:

apiVersion: mechanize.dev/v1alpha1
kind: SentinelRun
metadata:
  name: checkout-error-regression-20260801-1230
  namespace: observability
  ownerReferences:
    - apiVersion: mechanize.dev/v1alpha1
      kind: SentinelSchedule
      name: checkout-error-regression-hourly
      controller: true
spec:
  sentinelRef:
    name: post-deployment-error-regression-1-0-0
    digest: sha256:...
  variationRef:
    name: checkout-latency-regression
  inputs:
    service: checkout-api
  triggerReason: schedule          # schedule | manual | webhook
status:
  phase: Succeeded                 # Pending | Running | Succeeded | Failed | Cancelled
  startedAt: 2026-08-01T12:30:00Z
  completedAt: 2026-08-01T12:31:14Z
  exitCode: 0
  nodeStatuses:
    - id: get-deployment
      state: SUCCEEDED
      durationMs: 812
  findings:
    - type: deployment-error-regression
      severity: warning
      dedupeKey: checkout-api:deploy_abc
      evidenceRefs:
        - runs/<run-id>/evidence/deployment.json
  budgetsConsumed:
    llmTokens: 4210
    toolCalls: 6

RBAC roles (recommended, not normative):

* platform-admin: full control of all kinds; only role permitted to touch Sentinel and SentinelBinding
* sentinel-author: create/update Variation, create SentinelSchedule
* sentinel-operator: create SentinelRun on demand, view findings
* application-owner: view-only within namespace

The controller, CRDs, and admission webhook ship as a single Helm chart. First install requires cluster-admin; steady-state operation runs as a scoped ServiceAccount.

54. Sentinel repository (git) layout

The canonical durable home for authored Sentinels in v1 is a git repository — the "sentinels repo." GitOps controllers (Argo CD or Flux) sync from this repo into a cluster; mechanize can bootstrap and publish into it.

Recommended layout:

sentinels-repo/
  mechanize.yaml                  # repo-level config: clusters, registries, defaults
  sentinels/
    <name>/
      sentinel.yaml
      README.md
      audit-log.jsonl             # compiler decision log (§47)
      functions/
      fixtures/
      tests/
      replay/                     # optional; large runs may live in OCI only
      CODEOWNERS
  variations/
    <name>.yaml
  bindings/
    <cluster>/<namespace>/bindings.yaml
  schedules/
    <cluster>/<namespace>/<name>.yaml
  clusters/
    <cluster>/
      kustomization.yaml
      argocd-app.yaml
  .github/
    workflows/
      validate.yaml
      apply.yaml
    CODEOWNERS

Rules:

* Sentinel directories are additive-only in steady state. A rewrite is a new directory with a new digest suffix; the previous directory remains so historical SentinelRun objects can still resolve their base.
* Variations are single files sized for PR review (§21 already constrains their size).
* Bindings and schedules live under their cluster/namespace path. Ownership is obvious; GitOps overlays are trivial.
* audit-log.jsonl is retained so a reviewer can answer §44 questions without re-running the compiler.
* mechanize.yaml declares clusters, OCI registry, allowed providers, and default budgets.

Example mechanize.yaml:

apiVersion: mechanize.dev/v1alpha1
kind: RepoConfig
metadata:
  name: cardinalhq-sentinels
spec:
  registry:
    functions: ghcr.io/cardinalhq/mechanize-functions
    fixtures:  ghcr.io/cardinalhq/mechanize-fixtures
  clusters:
    - name: prod-observability
      context: prod
      namespace: observability
    - name: staging-observability
      context: staging
      namespace: observability
  defaults:
    execution:
      concurrency: 1
      failureMode: fail-fast
    budgets:
      llmTokens: 20000
      toolCalls: 20
  publishing:
    requirePRReview: true
    requireReplayBundle: true
    requireAuditLog: true
    autoMerge: false              # opt-in per Sentinel, not repo-wide

55. CI/CD scaffolding

Mechanize generates a CI/CD pipeline into a sentinels repo. v1 targets GitHub Actions. Other providers (GitLab CI, Jenkins) are out of v1 scope.

Command:

mechanize scaffold ci --target github-actions

This command writes workflow files to .github/workflows/, refuses to overwrite without --force, and prints every path it created or modified.

Two workflows are generated.

PR validation — .github/workflows/validate.yaml

Triggered by pull_request events touching sentinels/**, variations/**, bindings/**, or schedules/**.

Steps:

1. Install a pinned mechanize version.
2. Detect changed Sentinels, Variations, Bindings, and Schedules from the PR diff.
3. For each changed Sentinel:
   * mechanize validate (schema, graph, type, capability, portability, security);
   * mechanize replay on the recorded bundle;
   * fail if either step fails.
4. For each changed Variation:
   * resolve against its base;
   * validate the resolved DAG;
   * execute against declared fixtures if any.
5. For each changed Binding or Schedule:
   * kubectl apply --dry-run=server against the target cluster.
6. Post a PR comment summarizing per-artifact status, structural reuse metrics (§27) for changed Variations, and links to failed nodes.

Merge-to-main deployment — .github/workflows/apply.yaml

Triggered by push events to the default branch.

Steps:

1. Detect changed artifacts since the previous commit on the default branch.
2. Build and push function-node OCI images to the configured registry, tagged by content digest.
3. Materialize CR manifests into clusters/<cluster>/generated/.
4. Commit the generated manifests back, or push to a GitOps target branch, per repo configuration.
5. If Argo CD or Flux is configured, that controller applies. Otherwise CI runs kubectl apply -f against the configured contexts.
6. Wait for admission and reconciliation; fail the job if any manifest is rejected.

Neither workflow may bypass admission (§53 immutability, digest, redaction, and binding checks). Publishability requirements:

* replay bundle present (§20);
* audit log present (§47);
* README present;
* CODEOWNERS lists at least one non-bot approver;
* mechanize validate passed locally before PR open (recommended via repo-level pre-commit hook).

56. Publish and provisioning flow

End-to-end path from an authored Sentinel in .mechanize/ to a running SentinelSchedule in a production cluster.

1. Author locally.
   /mechanize compile writes .mechanize/sentinels/<name>/.
2. Validate locally.
   mechanize validate + mechanize replay must pass.
3. Prepare a PR against the sentinels repo.
   /mechanize publish or mechanize publish:
   * clone or open the configured sentinels repo;
   * copy the sentinel directory verbatim (including audit log, replay bundle, tests, fixtures);
   * open a PR through GitHub using the tools in §57;
   * attach the compiler rationale and structural reuse metrics as the PR body.
4. Human review.
   Reviewers answer the §44 questions using PR body + CI results (§55). Approval and merge are human actions; the skill does not auto-merge unless publishing.autoMerge is explicitly configured for the Sentinel.
5. Deploy on merge.
   apply.yaml builds OCI artifacts, materializes CRs, and applies to clusters (or triggers Argo CD/Flux sync).
6. Schedule.
   /mechanize schedule or mechanize schedule <sentinel-name> --cron "..." --cluster ... opens a follow-up PR adding a SentinelSchedule under schedules/<cluster>/<namespace>/.
7. Observe.
   SentinelRun status and findings are visible via kubectl and via any observability integration bound to the cluster.

Direct provisioning (no sentinels repo) is supported for local development and one-off experiments:

mechanize provision <sentinel-path> --context <kubectx> --namespace <ns>

This path skips CI and is not permitted against shared clusters. The admission webhook still applies.

57. GitHub integration in the /mechanize skill

The /mechanize skill publishes via GitHub using MCP tools already available to the skill's host agent — not a bespoke GitHub client. This keeps auth, rate limiting, and audit logging in one place.

Tools the skill invokes:

github.create_pr
github.comment
github.get_pr
github.list_prs
github.create_issue              # optional: on critical SentinelRun findings

Skill behavior:

* Confirm target repo and branch before opening a PR. Never open a PR against an unconfigured repo.
* Reuse an existing PR branch for iterative changes when responding to review comments. Do not open a new PR per revision.
* Attach compiler rationale, structural reuse metrics (§27), and an audit-log excerpt to the PR body — enough context for a reviewer to answer §44 without opening files.
* Never push to the default branch.
* Never approve or merge its own PR unless publishing.autoMerge is set for that Sentinel in mechanize.yaml. Auto-merge is opt-in per-Sentinel.

Publish flow UX:

1. Skill previews the PR title and body and asks the agent (and, if configured, the user) to confirm.
2. Skill enumerates the file diff it will push and asks for a second confirmation.
3. Skill opens the PR and returns the URL.
4. Skill polls github.get_pr up to a configurable timeout for CI to complete; posts a follow-up message with the pass/fail summary.
5. Skill exits. Merge is a human decision. The skill offers to open a scheduling PR only after merge is observed.

Non-goals for v1:

* No GitLab, Bitbucket, Gitea, or Azure DevOps support in the skill. The mechanize CLI may add these later; the skill is GitHub-only in v1.
* No GitHub App installation flow. Auth is handled by the MCP integration.
* No repository creation. The skill publishes to an existing repo; mechanize repo init bootstraps a new one out of band.

58. Productization prerequisites and v1 definition of done

Sections §53–§57 describe the v1 productization layer. Do not build any of it until the §51 evidence gates for v0 pass, and until these additional prerequisites are met:

1. At least three real investigations from at least two engineers have been compiled to Sentinels and executed on their captured replay bundles.
2. At least two Variations have been produced from real second investigations, and at least one has passed the human review protocol (§44).
3. At least one negative reuse case has correctly rejected a Sentinel candidate.
4. Users have voluntarily asked to schedule at least one Sentinel. If no one is asking, the CRD and controller are speculative infrastructure.
5. The compiler audit log (§47) is judged usable by an engineer who was not part of the original investigation.

Building the v1 layer before these gates are met trades certainty about the product hypothesis for volume of shipped code. §52 applies: the goal is preserving the largest semantically valid investigation procedure, not accumulating a workflow-execution platform.

v1 is complete when, in addition to v0 (§50), the repository contains:

* Kubernetes CRDs for Sentinel, Variation, SentinelBinding, SentinelSchedule, SentinelRun;
* controller image reconciling SentinelSchedule → SentinelRun → Pod;
* admission webhook enforcing immutability, digest, redaction, and binding checks;
* Helm chart installing CRDs, controller, and webhook;
* mechanize repo init, mechanize scaffold ci, mechanize provision, mechanize schedule, mechanize apply commands;
* GitHub Actions workflow templates for validate and apply;
* /mechanize publish and /mechanize schedule flows in at least one agent skill;
* one end-to-end pipeline test: local compile → PR against sentinels repo → CI validate → merge → CR applied → SentinelRun succeeds → finding emitted.