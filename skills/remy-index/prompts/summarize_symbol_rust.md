Task: Analyze the provided Rust source code and generate summaries for the specified target symbols in {lang}.

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
   - If information is primarily from a doc comment (/// or //!), use [Doc] prefix.
   - If the symbol is a struct definition, start with [Struct].
   - If the symbol is an enum definition, start with [Enum].
   - If the symbol is a trait definition, start with [Trait].
   - If the symbol is a type alias, start with [Typedef].
   - If the symbol is a macro_rules! definition, start with [Macro].
   - If the symbol is a module, start with [NS].
4. Only summarize symbols listed in "Target Symbols".
5. Leverage the provided "Dependencies" to understand the cross-file logic and data flow.
6. Pay attention to ownership and borrowing (&, &mut, move), lifetimes, and error handling via Result/Option and the ? operator.
7. Note trait implementations, generic bounds, async fns, and unsafe blocks when they define the symbol's contract.

Output Format (JSON):
[
  {{"name": "SymbolName", "summary": "Summary text..."}},
  ...
]

Examples:

Input:
- Target Symbols: ["queue_push"]
- Source Code: pub fn queue_push(q: &mut Queue, item: Item) -> Result<(), QueueError> {{ if q.data.len() == q.cap {{ return Err(QueueError::Full); }} q.data.push_back(item); Ok(()) }}

Output:
[{{"name": "queue_push", "summary": "Enqueue into bounded queue; Err if full."}}]

Input:
- Target Symbols: ["PacketHeader"]
- Source Code: pub struct PacketHeader {{ magic: u32, version: u16, len: u16 }}

Output:
[{{"name": "PacketHeader", "summary": "[Struct] Wire-format header: magic+version+len."}}]

Input:
- Target Symbols: ["Drawable"]
- Source Code: pub trait Drawable {{ fn draw(&self, canvas: &mut Canvas); }}

Output:
[{{"name": "Drawable", "summary": "[Trait] Render contract onto a canvas."}}]

Input:
- Target Symbols: ["log_line"]
- Source Code: macro_rules! log_line {{ ($lvl:expr, $msg:expr) => {{ eprintln!("[{{}}] {{}}", $lvl, $msg) }}; }}

Output:
[{{"name": "log_line", "summary": "[Macro] Leveled single-line stderr logging."}}]
