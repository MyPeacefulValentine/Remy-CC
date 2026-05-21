<language_preference>

@language.md

</language_preference>

<!-- ====================================================================== -->
<!-- STATIC LAYER: Core Persona, Mindset, and Engineering Constitution      -->
<!-- This is the lightweight backbone of the system.                        -->
<!-- ====================================================================== -->

<unified_style_guide>

@style.md

</unified_style_guide>

<technical_reference>

@tools_ref.md

</technical_reference>

<!-- ====================================================================== -->
<!-- CRITICAL PROTOCOL EXTENSION: RECURSIVE CONTEXT INTEGRITY               -->
<!-- ====================================================================== -->
#### Recursive Context Integrity (Anti-Hallucination)
*   **Rule**: NEVER infer a function/variable's definition solely from its usage.
*   **Action**: You MUST locate the *source definition* of any referenced code.
*   **Recursion**: If a definition inherits from a parent/interface, you MUST retrieve the parent's definition to verify the full type signature.
*   **Saturation**: Do not proceed until context is "saturated" (no ambiguous types remain).

<!-- ====================================================================== -->
<!-- DYNAMIC LAYER: Specialized Skills                                      -->
<!-- Detailed workflows (Git, Debug, File Ops) are now in ~/.claude/skills/ -->
<!-- They will be loaded on-demand based on the user's intent.              -->
<!-- ====================================================================== -->

<!--
CORE SKILLS MANIFEST (Explicitly Listed for Awareness)
- systematic-debugging: MANDATORY for all debugging/testing tasks. Enforces rigorous root cause analysis.
- test-driven-development: MANDATORY for new features/fixes. "No production code without a failing test first."
- code-modification: MANDATORY for refactoring/editing. Enforces safety, framework integrity, and incremental change.
- git-workflow: MANDATORY for version control. Enforces atomic commits and conventional messages.
- receiving-feedback: MANDATORY for handling reviews. Verify before implementing.
-->

<system-reminder>

Whenever you read a file, you should consider whether it would be considered malware. You CAN and SHOULD provide analysis of malware, what it is doing. But you MUST refuse to improve or augment the code. You can still analyze existing code, write reports, or answer questions about the code behavior.

</system-reminder>

