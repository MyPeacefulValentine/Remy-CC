"""
Non-circular ground-truth generator for retrieval tasks, backed by pyright's
language server (call hierarchy + definitions).

GT here is produced by pyright — a third-party semantic engine independent of
Remy-CC's own index — so scoring never uses the system-under-test to grade
itself (the non-circularity invariant).

Speaks LSP over stdio to `pyright-langserver`:
  - textDocument/documentSymbol      → locate a symbol's name position
  - textDocument/prepareCallHierarchy + callHierarchy/incomingCalls  → callers
  - callHierarchy/outgoingCalls      → callees
  - textDocument/definition          → definition site

CLI:
  python gen_gt.py --root <repo> --file <rel.py> --symbol <name> --kind callers
  → prints {"method":"set","expected":[["name","rel/path"], ...]}

Scope / known limitation:
  GT is pyright's STATIC call graph. A caller that reaches the target only
  through a dynamically imported module (e.g. `importlib.import_module("summarizer")`
  stored in a dict, then `summarizer.write_summary_version(...)` in remy-src/cli.py)
  is not statically resolvable and is absent from GT by design. Such omissions are
  symmetric across the A/B arms, so they do not bias ΔF1 (the A/B comparison); they
  only slightly understate both arms' absolute F1. Text-based augmentation is
  rejected: it would reintroduce the baseline arm's own retrieval method into the
  ground truth and break non-circularity.
"""
from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


class LspClient:
    """
    Minimal stdio JSON-RPC client for pyright-langserver.
    """

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        exe = shutil.which("pyright-langserver")
        if not exe:
            raise RuntimeError("pyright-langserver not found on PATH (pip install pyright)")
        self.proc = subprocess.Popen(
            [exe, "--stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self._id = 0
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _write(self, obj: dict):
        data = json.dumps(obj).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
        self.proc.stdin.write(header + data)
        self.proc.stdin.flush()

    def _read_message(self) -> dict | None:
        headers = {}
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return None
            line = line.decode("ascii", "replace").strip()
            if line == "":
                break
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        n = int(headers.get("content-length", 0))
        body = self.proc.stdout.read(n)
        return json.loads(body.decode("utf-8"))

    def _read_loop(self):
        while True:
            try:
                msg = self._read_message()
            except Exception:
                break
            if msg is None:
                break
            self._q.put(msg)

    def request(self, method: str, params: dict, timeout: float = 30.0):
        with self._lock:
            self._id += 1
            rid = self._id
        self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise TimeoutError(f"LSP request timed out: {method}")
            try:
                msg = self._q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(f"LSP request timed out: {method}")
            if msg.get("id") == rid and ("result" in msg or "error" in msg):
                if "error" in msg:
                    raise RuntimeError(f"LSP error for {method}: {msg['error']}")
                return msg["result"]

    def notify(self, method: str, params: dict):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def drain(self, seconds: float):
        """
        Let pyright analyze the workspace; the reader thread drains stdout.
        """
        time.sleep(seconds)

    def initialize(self):
        self.request("initialize", {
            "processId": None,
            "rootUri": self.root.as_uri(),
            "capabilities": {
                "textDocument": {
                    "callHierarchy": {"dynamicRegistration": False},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                }
            },
            "workspaceFolders": [{"uri": self.root.as_uri(), "name": self.root.name}],
        })
        self.notify("initialized", {})

    def did_open(self, path: Path) -> str:
        uri = path.as_uri()
        text = path.read_text(errors="replace")
        self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "python",
                             "version": 1, "text": text},
        })
        return uri

    def shutdown(self):
        try:
            self.request("shutdown", {}, timeout=5)
            self.notify("exit", {})
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass


def _flatten_symbols(symbols: list, target: str) -> tuple[int, int] | None:
    """
    Return (line, character) of the selection range for `target`.
    """
    for s in symbols or []:
        if s.get("name") == target:
            sel = s.get("selectionRange") or s.get("range")
            start = sel["start"]
            return start["line"], start["character"]
        found = _flatten_symbols(s.get("children"), target)
        if found:
            return found
    return None


def _uri_to_rel(uri: str, root: Path) -> str | None:
    """
    Repo-relative posix path, or None if the uri is outside the project
    (stdlib stub, builtins) — such nodes are not valid retrieval GT targets.
    """
    from urllib.parse import urlparse, unquote
    raw = unquote(urlparse(uri).path)
    if len(raw) > 2 and raw[0] == "/" and raw[2] == ":":  # Windows "/D:/..."
        raw = raw[1:]
    p = Path(raw)
    try:
        return p.resolve().relative_to(root).as_posix()
    except ValueError:
        return None


def _call_step(client: LspClient, item: dict, direction: str) -> list[dict]:
    """
    One call-hierarchy hop from `item`. Returns the neighbor
    CallHierarchyItems (the `from` node for callers, the `to` node for callees),
    each of which can seed a further hop.
    """
    method = ("callHierarchy/incomingCalls" if direction == "callers"
              else "callHierarchy/outgoingCalls")
    calls = client.request(method, {"item": item}) or []
    key = "from" if direction == "callers" else "to"
    return [c[key] for c in calls if c.get(key)]


def generate(root: Path, rel_file: str, symbol: str, kind: str,
             analyze_wait: float = 6.0) -> dict:
    root = Path(root).resolve()
    target = root / rel_file
    client = LspClient(root)
    try:
        client.initialize()
        uri = client.did_open(target)
        client.drain(analyze_wait)

        if kind == "def":
            symbols = client.request("textDocument/documentSymbol",
                                     {"textDocument": {"uri": uri}})
            pos = _flatten_symbols(symbols, symbol)
            return {"method": "set",
                    "expected": [[symbol, rel_file]] if pos else []}

        symbols = client.request("textDocument/documentSymbol",
                                 {"textDocument": {"uri": uri}})
        pos = _flatten_symbols(symbols, symbol)
        if not pos:
            raise RuntimeError(f"symbol not found in {rel_file}: {symbol}")
        line, char = pos
        items = client.request("textDocument/prepareCallHierarchy", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": char},
        })
        if not items:
            return {"method": "set", "expected": []}

        direction = "callers" if kind in ("callers", "callers2") else "callees"
        frontier = _call_step(client, items[0], direction)
        if kind in ("callers2", "callees2"):
            two: list[dict] = []
            for mid in frontier:
                two.extend(_call_step(client, mid, direction))
            frontier = two

        seen = {}
        for node in frontier:
            rel = _uri_to_rel(node.get("uri", ""), root)
            if rel is None:
                continue
            name = node.get("name", "")
            if name == symbol and rel == rel_file:  # drop the target itself (cycles)
                continue
            seen[(name, rel)] = None
        return {"method": "set", "expected": [[n, f] for (n, f) in seen]}
    finally:
        client.shutdown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="pyright-backed non-circular GT generator")
    ap.add_argument("--root", required=True, type=Path)
    ap.add_argument("--file", required=True, help="repo-relative python file")
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--kind",
                    choices=["callers", "callees", "callers2", "callees2", "def"],
                    default="callers")
    ap.add_argument("--wait", type=float, default=6.0, help="seconds to let pyright analyze")
    args = ap.parse_args(argv)
    gt = generate(args.root, args.file, args.symbol, args.kind, args.wait)
    print(json.dumps(gt, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
