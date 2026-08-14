"""
Language parsers for Logic Indexer.
Provides abstract interface and concrete implementations for Python, C, C++, TypeScript, and Rust.
"""

from .base import LanguageParser, SymbolInfo, EdgeInfo, ParserCacheIdentity
from .python_parser import PythonParser
from .c_cpp_parser import CCppParser
from .ts_parser import TSParser
from .rust_parser import RustParser
from .registry import ParserRegistry


def build_default_registry() -> ParserRegistry:
    """Create the standard registry with all built-in language parsers."""
    return ParserRegistry((PythonParser(), CCppParser(), TSParser(), RustParser()))


__all__ = [
    "LanguageParser", "SymbolInfo", "EdgeInfo", "ParserCacheIdentity",
    "PythonParser", "CCppParser", "TSParser", "RustParser",
    "ParserRegistry", "build_default_registry",
]
