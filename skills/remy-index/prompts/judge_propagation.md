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

``children`` lists only the child nodes whose summary text changed. ``old_summary``
is null when the child has no earlier summary text to compare against, which by
itself is not evidence of a contract change.

Examples:

Input:
{"parent_kind": "file", "parent_ref": "orders/service.py", "parent_summary": {"short": "Orchestrates checkout and payment capture.", "full": null}, "children": [{"child_ref": "orders/service.py::OrderService.checkout", "old_summary": {"short": "Return a bool for checkout success.", "full": null}, "new_summary": {"short": "Return OrderResult carrying status and id.", "full": null}}]}

Output:
{"propagate": true, "rationale": "Return type change is a signature break visible to callers.", "matched_dimension": "signature", "confidence": "high"}

Input:
{"parent_kind": "file", "parent_ref": "utils/sorting.py", "parent_summary": {"short": "In-place ordering helpers for record batches.", "full": null}, "children": [{"child_ref": "utils/sorting.py::sort_items", "old_summary": {"short": "Order records with a bubble pass.", "full": null}, "new_summary": {"short": "Order records via the standard library sort.", "full": null}}]}

Output:
{"propagate": false, "rationale": "Same-tier algorithm swap; observable contract unchanged.", "matched_dimension": "internal_refactor", "confidence": "high"}

Input:
{"parent_kind": "file", "parent_ref": "config/parser.py", "parent_summary": {"short": "Loads and validates configuration documents.", "full": null}, "children": [{"child_ref": "config/parser.py::_validate", "old_summary": null, "new_summary": {"short": "Check required and conditional fields.", "full": null}}]}

Output:
{"propagate": true, "rationale": "No baseline summary exists, so a new exception surface cannot be ruled out.", "matched_dimension": "ambiguous", "confidence": "low"}

Input:
{"parent_kind": "cluster", "parent_ref": "auth", "parent_summary": {"short": "Credential verification and session issuance.", "full": "[Role] Owns password checks and token minting.\n[API] authenticate(credentials) -> Session.\n[Inbound] api/middleware calls authenticate at request entry."}, "children": [{"child_ref": "auth/handler.py", "old_summary": {"short": "Compares MD5 digests against the user store.", "full": null}, "new_summary": {"short": "Verifies passwords with bcrypt against the user store.", "full": null}}]}

Output:
{"propagate": true, "rationale": "Hash algorithm change affects stored credential compatibility.", "matched_dimension": "security", "confidence": "high"}

Output JSON ONLY, matching this schema exactly:
{
  "propagate": true | false,
  "rationale": "<one sentence>",
  "matched_dimension": "<one of the dimensions above>",
  "confidence": "high" | "medium" | "low"
}
