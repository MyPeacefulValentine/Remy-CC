"""
Agent loop over an OpenAI-compatible chat/completions endpoint using native
multi-turn function-calling.

The target endpoint supports multi-turn tool use: round 1 returns
`finish_reason=tool_calls`, and a follow-up request carrying the assistant's
`tool_calls` message plus one `role: tool` result per call returns 200
(verified against the live endpoint). The loop sends the tool schemas on every
request, executes whatever tool_calls the model emits, appends the assistant
message and the tool results, and repeats until the model replies without
tool_calls or `max_turns` is reached.

The final answer is a fenced ```kbench block, so scoring stays objective and
independent of how the tools were called.

Reuses the urllib + retry/circuit-breaker approach from remy-index's run.py.

Instruments per run: tokens (prompt/completion/cached), tool-call counts by
name, wall time, turn count, an ordered tool_trace, and the final answer text.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Callable

_SYSTEM = """\
You are a precise code-retrieval agent working over a source tree.
Use the provided tools to find the answer; do NOT guess from memory. Call tools
as many times as needed — your first step should be a tool call.

When you have the final answer, reply with a fenced block in EXACTLY this format
(one item per line, `name|file`, path relative to the repo root):

```kbench
<symbol-or-function-name>|<repo/relative/path>
```

Rules:
- One line per item; use the real names and file paths the tools returned.
- If the answer is a single definition/location, emit one line.
- If you cannot determine the answer, emit an empty ```kbench block.
- Your FINAL message must contain ONLY the fenced ```kbench block and nothing
  else — no prose, no numbered lists, no explanation. Prose answers are not
  scored; only the fenced block is read.
- The ```kbench block is your final answer: emit it in a message with no tool
  calls, only when you are done.
"""

_NUDGE = ("Your last message had no ```kbench block. Emit your final answer NOW "
          "as a fenced ```kbench block — one `name|file` per line, repo-relative "
          "paths, no other text. Emit an empty block if you found nothing.")

_RETRYABLE = {500, 502, 503, 504}
_FATAL = {401, 403, 429}


def _post(url: str, api_key: str, payload: dict, timeout: int,
          retry_limit: int) -> dict:
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {api_key}"}
    body = json.dumps(payload).encode("utf-8")
    retries = 0
    while True:
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code in _FATAL:
                raise RuntimeError(f"fatal API error {e.code}: {detail}")
            if e.code in _RETRYABLE and retries < retry_limit:
                retries += 1
                time.sleep((2 ** retries) + random.random() * 0.3)
                continue
            raise RuntimeError(f"HTTP {e.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if retries < retry_limit:
                retries += 1
                time.sleep((2 ** retries) + random.random() * 0.3)
                continue
            raise RuntimeError(f"connection error: {e}")


def _parse_args(raw) -> dict:
    """Coerce a tool_call's arguments field (JSON string or object) to a dict."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def run_agent(prompt: str, tools: list[dict], dispatch: Callable[[str, dict], str],
              *, base_url: str, api_key: str, model: str,
              max_turns: int = 12, max_tokens: int = 1024,
              temperature: float = 0.2, timeout: int = 300,
              retry_limit: int = 3, obs_char_cap: int = 6000) -> dict:
    """Run one agent episode via native function-calling. Returns metrics + answer."""
    messages = [{"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt}]
    tokens_in = tokens_out = tokens_cache = 0
    tool_calls: dict[str, int] = {}
    tool_trace: list[dict] = []
    turns = 0
    answer = ""
    nudged = False
    t0 = time.time()

    while turns < max_turns:
        payload = {"model": model, "messages": messages, "tools": tools,
                   "tool_choice": "auto", "max_tokens": max_tokens,
                   "temperature": temperature}
        resp = _post(base_url, api_key, payload, timeout, retry_limit)
        turns += 1

        usage = resp.get("usage") or {}
        tokens_in += usage.get("prompt_tokens", 0) or 0
        tokens_out += usage.get("completion_tokens", 0) or 0
        details = usage.get("prompt_tokens_details") or {}
        tokens_cache += details.get("cached_tokens", 0) or 0

        msg = ((resp.get("choices") or [{}])[0].get("message")) or {}
        tcs = msg.get("tool_calls") or []

        asst = {"role": "assistant", "content": msg.get("content") or ""}
        if tcs:
            asst["tool_calls"] = tcs
        messages.append(asst)

        if not tcs:
            content = msg.get("content") or ""
            # One-shot recovery: if the model stopped without the fenced block,
            # ask once for the contract format instead of scoring a prose answer
            # as an empty (F1=0) result.
            if "```kbench" not in content and not nudged and turns < max_turns:
                nudged = True
                messages.append({"role": "user", "content": _NUDGE})
                continue
            answer = content
            break

        for tc in tcs:
            fn = tc.get("function") or {}
            name = fn.get("name") or ""
            args = _parse_args(fn.get("arguments"))
            tool_calls[name] = tool_calls.get(name, 0) + 1
            tool_trace.append({"turn": turns, "name": name, "input": args})
            out = dispatch(name, args)[:obs_char_cap]
            messages.append({"role": "tool", "tool_call_id": tc.get("id"),
                             "content": out})

    return {
        "answer": answer,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_cache": tokens_cache,
        "tool_calls": tool_calls,
        "tool_trace": tool_trace,
        "wall_seconds": round(time.time() - t0, 2),
        "turns": turns,
    }
