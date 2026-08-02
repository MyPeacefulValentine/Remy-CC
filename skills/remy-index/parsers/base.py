"""
Abstract base class for language-specific parsers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
import importlib
import json
from typing import Mapping, Optional


@dataclass(frozen=True)
class ParserCacheIdentity:
    """Persistent identity for structure facts produced by a parser."""
    contract_version: str
    backend: str
    environment: str

    @classmethod
    def create(cls, contract_version: str, backend: str, environment: Optional[Mapping[str, str]] = None):
        encoded = json.dumps(
            dict(environment or {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(contract_version, backend, encoded)

    def as_db_tuple(self):
        return self.contract_version, self.backend, self.environment


def distribution_version(distribution: str) -> str:
    """Return installed package metadata without making it an import dependency."""
    try:
        metadata = importlib.import_module("importlib.metadata")
    except ImportError:
        try:
            metadata = importlib.import_module("importlib_metadata")
        except ImportError as exc:
            raise RuntimeError("Package metadata API is unavailable") from exc
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError as exc:
        raise RuntimeError(
            f"Package metadata is unavailable for {distribution}"
        ) from exc


@dataclass
class SymbolInfo:
    """Represents a single extracted code symbol."""
    name: str
    args: str               # e.g., "(int a, float b)" or "(self, x)"
    type: str               # "function", "class", "struct", "enum", "typedef", "macro", "namespace", "interface", "type_alias"
    lineno: int
    source_segment: str
    end_lineno: Optional[int] = None
    docstring: Optional[str] = None
    bases: Optional[list] = None


@dataclass
class EdgeInfo:
    """Represents a caller-to-callee relationship within a file."""
    caller: str
    callee: str
    line: int
    provenance: Optional[str] = None
    synthesized_from: Optional[str] = None
    via: Optional[str] = None


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

    @abstractmethod
    def cache_identity(self, source: str, file_path: str) -> ParserCacheIdentity:
        """Return the identity of structure facts generated for this source."""

    @abstractmethod
    def cache_identity_candidates(self, file_path: str) -> tuple:
        """Return possible current identities without reading source content."""

    def matches(self, filename: str) -> bool:
        """Check if this parser handles the given filename."""
        return any(filename.endswith(ext) for ext in self.get_extensions())

    def extract_patterns(self, source: str, file_path: str) -> list:
        """Extract event/callback registration patterns from source.
        Returns list of dicts: {pattern_type, signal_name, handler, line, metadata}.
        """
        return []

    def extract_call_graph(self, source: str, file_path: str) -> list:
        """Extract caller-to-callee edges. Returns list of EdgeInfo. Override in subclasses with AST/tree-sitter support."""
        return []
