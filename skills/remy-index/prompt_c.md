Task: Analyze the provided C/C++ source code and generate summaries for the specified target symbols in {lang}.

Input Data:
- Target Symbols: {target_symbols}
- Dependencies (Context):
{context_summaries}
- Source Code:
{source_code}

Constraints:
1. Return a JSON Array of objects.
2. Each object must have "name" (symbol name) and "summary" (string).
3. Summary Rules:
   - One sentence only.
   - Max 50 characters.
   - No symbol name repetition.
   - Focus on responsibility/intent.
   - If it's a test function, start with [Test].
   - If it's a utility, start with [Util].
   - If a function acts as a Data Source (file read, network receive, device input), use [Source] prefix.
   - If a function acts as a Data Sink (file write, network send, device output), use [Sink] prefix.
   - If information is primarily from a Doxygen comment, use [Doc] prefix.
   - If the symbol is a struct definition, start with [Struct].
   - If the symbol is an enum definition, start with [Enum].
   - If the symbol is a typedef, start with [Typedef].
   - If the symbol is a function-like macro, start with [Macro].
   - If the symbol is a namespace, start with [NS].
   - If the symbol is a C++ class, start with [Class].
4. Only summarize symbols listed in "Target Symbols".
5. Leverage the provided "Dependencies" to understand the cross-file logic and data flow.
6. For C code: pay attention to pointer ownership, memory allocation/free patterns, and struct field usage.
7. For C++ code: note inheritance relationships, virtual dispatch, RAII patterns, and template usage.

Output Format (JSON):
[
  {{"name": "SymbolName", "summary": "Summary text..."}},
  ...
]
