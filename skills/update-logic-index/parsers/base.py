"""
Abstract base class for language-specific parsers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SymbolInfo:
    """Represents a single extracted code symbol."""
    name: str
    args: str               # e.g., "(int a, float b)" or "(self, x)"
    type: str               # "function", "class", "struct", "enum", "typedef", "macro", "namespace", "interface", "type_alias"
    lineno: int
    source_segment: str
    docstring: Optional[str] = None  # For class/struct methods


@dataclass
class EdgeInfo:
    """Represents a caller-to-callee relationship within a file."""
    caller: str
    callee: str
    line: int


class LanguageParser(ABC):
    """Abstract interface for language-specific code parsing."""

    @abstractmethod
    def get_extensions(self) -> list:
        """Return file extensions this parser handles, e.g. ['.py'] or ['.c', '.h']."""

    @abstractmethod
    def parse_symbols(self, source: str, file_path: str) -> list:
        """
        Extract top-level symbols from source code.
        Returns list of SymbolInfo.
        """

    @abstractmethod
    def resolve_imports(self, source: str, file_path: str, root_dir: str) -> dict:
        """
        Resolve internal imports/includes.
        Returns {relative_path: has_alias} for internal dependencies.
        """

    @abstractmethod
    def collect_used_names(self, source: str) -> set:
        """Collect identifiers referenced in the source."""

    @abstractmethod
    def get_complexity_indicators(self) -> list:
        """Return substrings that indicate complex/dynamic code patterns."""

    @abstractmethod
    def get_prompt_template_path(self) -> str:
        """Return absolute path to the LLM prompt template for this language."""

    def matches(self, filename: str) -> bool:
        """Check if this parser handles the given filename."""
        return any(filename.endswith(ext) for ext in self.get_extensions())

    def extract_call_graph(self, source: str, file_path: str) -> list:
        """Extract caller-to-callee edges. Returns list of EdgeInfo. Override in subclasses with AST/tree-sitter support."""
        return []
