Task: Analyze the provided Python source code and generate summaries for the specified target symbols in {lang}.

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
   - No function name repetition.
   - Focus on responsibility/intent.
   - If it's a test function, start with [Test].
   - If it's a utility, start with [Util].
   - If a function acts as a Data Source, use [Source] prefix.
   - If a function acts as a Data Sink, use [Sink] prefix.
   - If information is primarily from docstring, use [Doc] prefix.
4. Only summarize symbols listed in "Target Symbols".
5. Leverage the provided "Dependencies" to understand the cross-file logic and data flow.

Output Format (JSON):
[
  {{"name": "SymbolName", "summary": "Summary text..."}},
  ...
]

Examples:

Input:
- Target Symbols: ["verify_token"]
- Source Code: def verify_token(token): payload = _decode(token); _check_expiry(payload); return payload

Output:
[{{"name": "verify_token", "summary": "Reject expired or tampered JWTs."}}]

Input:
- Target Symbols: ["test_login_returns_200"]
- Source Code: def test_login_returns_200(client): resp = client.post("/login", ...); assert resp.status_code == 200

Output:
[{{"name": "test_login_returns_200", "summary": "[Test] Asserts 200 on valid credentials."}}]

Input:
- Target Symbols: ["read_yaml"]
- Source Code: def read_yaml(path): return yaml.safe_load(open(path))

Output:
[{{"name": "read_yaml", "summary": "[Source] Load YAML config from disk."}}]
