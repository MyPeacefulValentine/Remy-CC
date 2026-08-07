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

``symbol_summaries`` carries only the symbols that already hold a summary, so it
may be shorter than the file's full symbol list. ``imports`` lists project-internal
file paths this file depends on.

Examples:

Input (kind_hint=cohesive):
{"file_path": "auth/jwt_verifier.py", "kind_hint": "cohesive", "symbol_summaries": [{"name": "verify_token", "short": "Reject expired or tampered JWTs."}, {"name": "_decode", "short": "[Util] Split and base64-decode the token."}, {"name": "_check_expiry", "short": "Compare exp claim against current time."}], "imports": ["auth/keyring.py", "common/clock.py"]}

Output:
{"short": "Verifies JWT tokens and rejects expired or tampered payloads.", "full": null}

Input (kind_hint=low_cohesion):
{"file_path": "utils/string_helpers.py", "kind_hint": "low_cohesion", "symbol_summaries": [{"name": "slugify", "short": "[Util] Normalize text into a URL slug."}, {"name": "truncate", "short": "[Util] Cut text at a boundary with ellipsis."}, {"name": "strip_html", "short": "[Util] Remove markup and unescape entities."}, {"name": "camel_to_snake", "short": "[Util] Convert camelCase into snake_case."}], "imports": []}

Output:
{"short": "Heterogeneous string utilities (slug/truncate/sanitize/case).", "full": "[Format] slugify, truncate; [Sanitize] strip_html; [Naming] camel_to_snake."}

Input (kind_hint=trivial):
{"file_path": "http/constants.py", "kind_hint": "trivial", "symbol_summaries": [{"name": "default_timeouts", "short": "Return the connect/read timeout pair."}], "imports": []}

Output:
{"short": "Module-level retry/timeout constants for the HTTP client.", "full": null}

Input (kind_hint=abstract):
{"file_path": "storage/base.py", "kind_hint": "abstract", "symbol_summaries": [{"name": "StorageBackend", "short": "Abstract get/put/delete contract."}, {"name": "StorageBackend.transaction", "short": "Yield a transactional scope to callers."}, {"name": "TransactionContext", "short": "Track pending writes until commit."}], "imports": ["storage/errors.py"]}

Output:
{"short": "Abstract storage contract: get/put/transaction with default retry wiring.", "full": null}

Input (kind_hint=schema):
{"file_path": "models/order.py", "kind_hint": "schema", "symbol_summaries": [{"name": "Order", "short": "Order header with customer and total."}, {"name": "OrderItem", "short": "Line item with sku, qty and price."}, {"name": "OrderStatus", "short": "Lifecycle enum for order state."}], "imports": ["models/base.py"]}

Output:
{"short": "Order schema: header + line items + lifecycle status enum.", "full": "Order(id, customer_id, status, total); OrderItem(order_id, sku, qty, price); OrderStatus enum (DRAFT/PAID/SHIPPED/CANCELLED)."}

Input (kind_hint=entry):
{"file_path": "cli/main.py", "kind_hint": "entry", "symbol_summaries": [{"name": "main", "short": "Parse argv and dispatch to a handler."}, {"name": "_parse_args", "short": "[Util] Build the argument parser."}, {"name": "_setup_logging", "short": "[Sink] Configure handlers and levels."}], "imports": ["cli/commands.py", "common/logging_config.py"]}

Output:
{"short": "CLI entry: parse argv, init logging, dispatch to subcommand handler.", "full": null}

Constraints:
1. Return a single JSON object: ``{"short": "...", "full": "..." | null}``.
2. ``short`` MUST be <= {{char_limit_short}} characters.
3. ``full`` (when not null) MUST be <= {{char_limit_full}} characters total.
4. {{strict_note}}
5. Focus on observable contract; do not restate symbol names verbatim.
6. Output JSON only, no surrounding prose.
