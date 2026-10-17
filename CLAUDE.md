<language_preference>

@language.md

</language_preference>

<unified_style_guide>

@style.md

</unified_style_guide>

<technical_reference>

@tools_ref.md

</technical_reference>

## Code investigation

Do not infer implementation from usage alone. Inspect the definitions, interfaces, inheritance, and callers that can change the current decision. Stop expanding when the task-relevant behavior is understood; identify unresolved assumptions instead of claiming that every dependency or type is known. The shared skill procedure is `skills/remy-plan/saturation_protocol.md`.

## Security analysis

When reading code, consider whether it is malware. Analysis, reports, and explanations are permitted; do not improve or augment malware.
