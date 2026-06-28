Task: Decide whether a parent-node summary must be rewritten given a list of child changes.

Input:
{{payload}}

Sensitive dimensions (output ``propagate: true`` when matched):
- ``signature``: public signature, return type, visibility — callers must change
- ``error_contract``: thrown exceptions, error codes, retry semantics
- ``side_effects``: I/O, network, persistence, shared-state mutation
- ``concurrency``: sync/async, locking, reentrancy
- ``resource_lifecycle``: allocation, handles, connection management
- ``complexity_tier``: time/space complexity tier crossed
- ``security``: auth, input sanitization, secret handling
- ``data_contract``: schema, encoding, units, serialization compat

Absorbed dimensions (output ``propagate: false`` when matched):
- ``internal_refactor``: same-tier implementation swap (bubble vs quicksort, hashmap vs btreemap)
- ``rename``: identifier rename not affecting the public surface
- ``extract_inline``: extract/inline function, constant extraction
- ``comment_only``: comments, whitespace, formatting
- ``test_only``: test additions or refactors not changing public behaviour
- ``log_only``: log output wording changes

Conservative bias (mandatory when uncertain):
- If you cannot decide with confidence, output ``propagate: true`` with
  ``matched_dimension: 'ambiguous'`` and ``confidence: 'low'``.
- False negatives (failure to propagate) are worse than false positives
  (one extra rewrite).

Examples:

Input:
{"parent": "OrderService.checkout", "child_changes": [{"symbol": "OrderService.checkout", "diff_summary": "Return type changed from bool to OrderResult"}]}

Output:
{"propagate": true, "rationale": "Return type change is a signature break visible to callers.", "matched_dimension": "signature", "confidence": "high"}

Input:
{"parent": "sort_items", "child_changes": [{"symbol": "sort_items", "diff_summary": "Replaced bubble sort with std::sort; same O(n log n) tier"}]}

Output:
{"propagate": false, "rationale": "Same-tier algorithm swap; observable contract unchanged.", "matched_dimension": "internal_refactor", "confidence": "high"}

Input:
{"parent": "ParseConfig.load", "child_changes": [{"symbol": "_validate", "diff_summary": "Added new field validation; behavior unclear from diff"}]}

Output:
{"propagate": true, "rationale": "Validation change may surface as new exceptions to callers.", "matched_dimension": "ambiguous", "confidence": "low"}

Input:
{"parent": "AuthHandler.authenticate", "child_changes": [{"symbol": "_check_password", "diff_summary": "Switched from MD5 to bcrypt"}]}

Output:
{"propagate": true, "rationale": "Hash algorithm change affects stored credential compatibility.", "matched_dimension": "security", "confidence": "high"}

Output JSON ONLY, matching this schema exactly:
{
  "propagate": true | false,
  "rationale": "<one sentence>",
  "matched_dimension": "<one of the dimensions above>",
  "confidence": "high" | "medium" | "low"
}
