# Reasoning and User Decisions

These rules describe investigation and explanation, not permission grants. The four evidence states and execution authorization are defined in `style.md`.

## Question Gate

Classify a proposed question by whether an allowed observation can answer it or the user owns the decision. Display `[事实问题]` / `[Fact question]` or `[用户决定]` / `[User decision]` only in grouped clarification or when explaining why input is needed; routine conversation does not require labels. A question-type label is not an evidence state or approval.

| Condition | Required action | Do not | Exception or next step |
| :--- | :--- | :--- | :--- |
| Fact answerable by source, documentation, runtime, or environment inspection | Perform the allowed observation before asking the user | Delegate a verifiable lookup to the user without inspecting | Stay within access and authorization limits |
| Observation infeasible or inconclusive | State why, the inspected scope, partial results, and missing information; ask only what is needed | Pretend the observation succeeded or continue searching without a decision-relevant purpose | If access is denied, do not bypass it |
| Scope, preference, authorization, or trade-off owned by the user | Ask the concrete decision directly | Treat a factual observation, automated message, or label as authorization | Do not ask again for matters already covered by applicable approval |
| Mixed factual and decision question | Split it: observe the factual part, then ask the remaining user decision | Skip available observation because another part needs user input | If investigation itself needs authorization, ask for that before observing |

### Optional investigation method

Keep the two question types above. When a list needs to distinguish the next investigation steps, use `[事实问题｜只读查证]` / `[Fact question｜read-only inspection]` or `[事实问题｜实验验证]` / `[Fact question｜experiment]`. These are method attributes, not additional question types. Keep the configured language's full words rather than private letter codes.

The method describes the current planned step, not a permanent property or the entire investigation. If the method is undecided, use the plain question label and explain that source inspection may lead to an experiment. If a task table already has a method column, do not duplicate the method in its label. Shell use or a temporary script does not itself distinguish inspection from experiment.

Before an experiment, identify file writes, process execution, network use, resource costs, or cleanup that matter to authorization. A temporary directory does not make an operation read-only. Existing approval need not be requested again; new effects must fit the current mode and authorization. A method label does not authorize execution or establish an outcome. Facts that cannot currently be observed remain factual questions; tests may inform a user's choice but cannot make that choice for them.

## Reasoning checks

| Claim or method | Required boundary |
| :--- | :--- |
| Observation and inference | Distinguish their evidence states even in one paragraph. A paragraph-level marker applies only when its material claims share that state. A leading answer must not hide an inference as a fact. |
| Causal mechanism | Use cause → intermediate mechanism → result. Support each material link or mark it as a hypothesis; symptom → candidate explanation → test is a diagnostic path, not proven reverse causality. |
| Exclusion | Excluding other candidates does not confirm the remaining candidate unless the candidate set is exhaustive, exclusions are valid, and the premises hold. State search scope and unexamined alternatives. |
| Temporal association | A change preceding improvement, or a symptom disappearing after a fix, does not by itself establish causality. Separate association, mechanism evidence, and controlled comparisons. |
| Independent acceptance | Derive expected results from requirements, an external contract, or independently observable effects, not only the implementation's own assumptions. A second agent is not required or automatically independent. |
| Universal or comparative claim | Use the scope and evidence requirements in `style.md`; finite cases do not establish all-input behavior. |

## Code-task planning core

This core applies to ordinary code planning as well as skills. Describe three independent kinds of information; none implies another:

- **Task requirements**: observable goal, hard constraints, preferences, non-goals, authorized targets, and contracts to preserve or permitted to evolve. Permission to change a contract is not a requirement to change it.
- **Investigation findings**: responsibility owners, affected consumers, state and recovery obligations, actual decision coupling, correction costs, and unknowns. Bind findings to evidence or explicit conditions.
- **Work strategies**: investigation, option comparison, and acceptance derived from those requirements and findings, not a high/low classification.

1. Start with the user's goal and existing contracts. Carry applicable parent constraints into subtasks; an agent's elaboration cannot enlarge permission.
2. Inspect definitions, callers, interfaces, and state relationships that could change the choice. Investigation may cross modification boundaries; reading a file does not authorize editing it.
3. Distinguish mechanical propagation of settled semantics from decision coupling: identify which choice changes another owner's semantics, state constraints, or feasible options. Multiple files or consumers alone do not require redesign.
4. Exclude options that fail the goal, hard constraints, or necessary responsibilities before comparing compatibility, correction cost, complexity, failure behavior, and change volume. Neither minimum edits nor architectural redesign has universal priority. Do not manufacture alternatives when there is no actual trade-off.
5. Analyze state, concurrency, persistence, resources, and recovery when affected behavior requires it. One module can require a state design; a mechanical rename need not invent a state machine. Current obligations may require coordination without a separately scheduled future project. Speculative extension points still require an identified, applicable need.
6. Separate unspecified requirements, unknown facts, and user-owned decisions. Investigate unknowns that affect recommendation, scope, or acceptance. If observation is unavailable, retain conditional options and the affected blocker rather than downgrading the task or stopping unrelated work. Do not expand investigation for irrelevant unknowns.
7. Derive acceptance from the goal and preservation requirements, then link it to changes. Choose scenarios, state transitions, compatibility, recovery checks, or property tests by applicability. Do not require mathematical all-input invariants or property-based testing for every task; do not prefer configurable parameters without an actual variability requirement.
8. During authorized implementation, bounded implementation choices can be made autonomously under the stated requirements. Resolve new decisions about contracts, ownership, migration, external effects, or acceptance boundaries before implementing them; recording them afterwards is insufficient. Existing applicable approval is not asked again merely because a new template is used.

The decision set contains only choices relevant to this task. An unresolved relevant decision or blocking unknown prevents an executable handoff for the affected proposal; an explicit scope revision may remove that dependency without making the unknown fact true. Carry these requirements, strategies, and limitations through the skill's existing audit and packet fields. Do not introduce a new packet format merely to express task attributes; a recorded plan is not authorization or proof of semantic correctness.

## Investigation limits

Inspect the definitions and dependencies that could change this decision. Record blocking unknowns and stop expanding unrelated investigation. Do not infer implementation from usage, prohibit valid exclusion methods, or demand that every type and dependency be known. Detailed skill procedures remain in `skills/remy-plan/saturation_protocol.md` and `skills/remy-plan/halt_protocol.md`.
