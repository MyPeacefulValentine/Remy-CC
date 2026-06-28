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

Examples:

Input:
- Target Symbols: ["queue_push"]
- Source Code: int queue_push(queue_t *q, void *item) { if (!q || q->size == q->cap) return -ENOMEM; q->data[q->tail++] = item; return 0; }

Output:
[{{"name": "queue_push", "summary": "Enqueue into bounded ring; -ENOMEM if full."}}]

Input:
- Target Symbols: ["packet_header"]
- Source Code: struct packet_header {{ uint32_t magic; uint16_t version; uint16_t len; }};

Output:
[{{"name": "packet_header", "summary": "[Struct] Wire-format header: magic+version+len."}}]

Input:
- Target Symbols: ["LIST_FOR_EACH"]
- Source Code: #define LIST_FOR_EACH(p, head) for (p = (head)->next; p != (head); p = p->next)

Output:
[{{"name": "LIST_FOR_EACH", "summary": "[Macro] Forward-iterate over circular list."}}]

Input:
- Target Symbols: ["BufferedReader"]
- Source Code: class BufferedReader : public Reader {{ public: explicit BufferedReader(Reader& src, size_t cap); ssize_t read(char* dst, size_t n) override; private: std::unique_ptr<char[]> buf_; }};

Output:
[{{"name": "BufferedReader", "summary": "[Class] RAII-buffered wrapper over Reader."}}]
