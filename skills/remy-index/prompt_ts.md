Task: Analyze the provided TypeScript/TSX source code and generate summaries for the specified target symbols in {lang}.

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
   - If a function acts as a Data Source (fetch, read, subscribe), use [Source] prefix.
   - If a function acts as a Data Sink (write, send, dispatch), use [Sink] prefix.
   - If information is primarily from a JSDoc comment, use [Doc] prefix.
   - If the symbol is an interface declaration, start with [Interface].
   - If the symbol is a type alias declaration, start with [TypeAlias].
   - If the symbol is an enum declaration, start with [Enum].
   - If the symbol is a namespace, start with [NS].
   - If the symbol is a class, start with [Class].
4. Only summarize symbols listed in "Target Symbols".
5. Leverage the provided "Dependencies" to understand the cross-file logic and data flow.
6. For TypeScript: note generic type constraints, async/await patterns, and decorator usage where relevant.
7. For TSX: note React component prop contracts and JSX return patterns where relevant.

Output Format (JSON):
[
  {{"name": "SymbolName", "summary": "Summary text..."}},
  ...
]
