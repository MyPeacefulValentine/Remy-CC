Task: Summarize the contract of a single source file based on its symbol-level summaries.

Input:
{{payload}}

The ``kind_hint`` field routes the writing style. Apply the matching block:

{% if kind_hint == 'cohesive' %}
The file is a cohesive module with a single responsibility. Write one short
sentence describing the file's contract — what it provides and where it sits
in collaboration with other files. Do NOT enumerate functions. Output the
``full`` field as null.
{% endif %}

{% if kind_hint == 'low_cohesion' %}
The file is a utility collection holding heterogeneous functions. Produce a
``short`` line (one-sentence positioning) AND a ``full`` description grouping
functions by category. Each category entry must be a brief locator, not a
full doc.
{% endif %}

{% if kind_hint == 'trivial' %}
The file holds few small symbols. Treat it as cohesive and produce a single
sentence describing its purpose. Output ``full`` as null.
{% endif %}

{% if kind_hint == 'abstract' %}
The file declares abstract base classes or interfaces. Describe the
abstraction level, the contract expected of implementers, and any default
behaviour provided. Output ``full`` as null.
{% endif %}

{% if kind_hint == 'schema' %}
The file declares data models or schemas. List the principal entities and
their field roles. Output ``short`` as a one-sentence positioning and
``full`` as the entity inventory.
{% endif %}

{% if kind_hint == 'entry' %}
The file is an entry script or main program. Describe the startup flow,
top-level side effects, and CLI surface. Output ``full`` as null.
{% endif %}

Examples:

Input (kind_hint=cohesive):
{"path": "auth/jwt_verifier.py", "symbols": ["verify_token", "_decode", "_check_expiry"], "kind_hint": "cohesive"}

Output:
{"short": "Verifies JWT tokens and rejects expired or tampered payloads.", "full": null}

Input (kind_hint=low_cohesion):
{"path": "utils/string_helpers.py", "symbols": ["slugify", "truncate", "strip_html", "camel_to_snake"], "kind_hint": "low_cohesion"}

Output:
{"short": "Heterogeneous string utilities (slug/truncate/sanitize/case).", "full": "[Format] slugify, truncate; [Sanitize] strip_html; [Naming] camel_to_snake."}

Input (kind_hint=trivial):
{"path": "constants.py", "symbols": ["MAX_RETRIES", "DEFAULT_TIMEOUT"], "kind_hint": "trivial"}

Output:
{"short": "Module-level retry/timeout constants for the HTTP client.", "full": null}

Input (kind_hint=abstract):
{"path": "storage/base.py", "symbols": ["StorageBackend", "TransactionContext"], "kind_hint": "abstract"}

Output:
{"short": "Abstract storage contract: get/put/transaction with default retry wiring.", "full": null}

Input (kind_hint=schema):
{"path": "models/order.py", "symbols": ["Order", "OrderItem", "OrderStatus"], "kind_hint": "schema"}

Output:
{"short": "Order schema: header + line items + lifecycle status enum.", "full": "Order(id, customer_id, status, total); OrderItem(order_id, sku, qty, price); OrderStatus enum (DRAFT/PAID/SHIPPED/CANCELLED)."}

Input (kind_hint=entry):
{"path": "cli/main.py", "symbols": ["main", "_parse_args", "_setup_logging"], "kind_hint": "entry"}

Output:
{"short": "CLI entry: parse argv, init logging, dispatch to subcommand handler.", "full": null}

Constraints:
1. Return a single JSON object: ``{"short": "...", "full": "..." | null}``.
2. ``short`` MUST be <= {{char_limit_short}} characters.
3. ``full`` (when not null) MUST be <= {{char_limit_full}} characters total.
4. {{strict_note}}
5. Focus on observable contract; do not restate symbol names verbatim.
6. Output JSON only, no surrounding prose.
