Task: Summarize the contract of a cluster (subsystem) based on its file-level summaries.

Input:
{{payload}}

Output two forms:
- ``short``: one-sentence positioning, <= {{char_limit_short}} characters,
  no tags, suitable for inline display in a cluster overview table.
- ``full``: a structured description with three tagged sections:
  - ``{{tag_position}}`` what the subsystem is and what role it plays
  - ``{{tag_api}}`` the entry surface (use ``entry_symbols`` and file API)
  - ``{{tag_deps}}`` which other clusters call into this one (use
    ``inbound_clusters``); say "{{empty_inbound_phrase}}" if empty

Examples (rendered with REMY_LANG=en; zh-CN substitutes the Chinese tag set automatically):

Input (cluster with inbound):
{"cluster_name": "auth/gateway", "file_summaries": [{"file": "auth/jwt_verifier.py", "short": "Verify JWTs."}, {"file": "auth/token_issuer.py", "short": "Mint signed tokens."}], "entry_symbols": ["verify_token", "issue_token"], "inbound_clusters": ["api/middleware", "background/worker"]}

Output:
{"short": "Gateway-layer auth and token issuance.", "full": "[Role] Centralizes identity verification, token issuance and renewal; hides crypto details from callers.\n[API] verify_token(token) -> Identity; issue_token(user) -> str.\n[Inbound] api/middleware calls verify_token at request entry; background/worker calls issue_token to renew within async jobs."}

Input (cluster without inbound):
{"cluster_name": "tools/internal_codegen", "file_summaries": [{"file": "tools/gen_schema.py", "short": "Render JSON schema from models."}], "entry_symbols": ["render"], "inbound_clusters": []}

Output:
{"short": "Build-time schema code generation tool.", "full": "[Role] Offline script triggered by the build pipeline to emit JSON schema files.\n[API] render() -> None (writes dist/schema/*.json).\n[Inbound] No external callers."}

Constraints:
1. Return a single JSON object: ``{"short": "...", "full": "..."}``.
2. ``full`` MUST be <= {{char_limit_full}} characters total (including tags).
3. {{strict_note}}
4. Do not enumerate every file; group them by contract.
5. Output JSON only, no surrounding prose.
