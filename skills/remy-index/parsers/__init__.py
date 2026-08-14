"""
Language parsers for Logic Indexer.
Provides abstract interface and concrete implementations for Python, C, C++, and TypeScript.
"""

from .base import LanguageParser, SymbolInfo, EdgeInfo, ParserCacheIdentity
from .python_parser import PythonParser
from .c_cpp_parser import CCppParser
from .ts_parser import TSParser
from .registry import ParserRegistry


def build_default_registry() -> ParserRegistry:
    """Create the standard registry with all built-in language parsers."""
    return ParserRegistry((PythonParser(), CCppParser(), TSParser()))


__all__ = [
    "LanguageParser", "SymbolInfo", "EdgeInfo", "ParserCacheIdentity",
    "PythonParser", "CCppParser", "TSParser",
    "ParserRegistry", "build_default_registry",
]
