"""Immutable parser registry with validation and longest-suffix resolution."""

import types
from typing import Iterable, Optional

from .base import LanguageParser


class ParserRegistry:
    """Validated, immutable collection of language parsers.

    Construction validates that every parser has a non-empty unique
    ``language_id``, that every extension starts with ``'.'`` and is
    registered by at most one parser.  After validation the registry is
    sealed — no mutations are possible.

    Resolution uses longest-suffix matching so that a multi-segment
    extension such as ``.d.ts`` would take precedence over ``.ts`` if
    both were registered.
    """

    def __init__(self, parsers: Iterable[LanguageParser]) -> None:
        parser_tuple = tuple(parsers)
        seen_ids: dict[str, LanguageParser] = {}
        extension_map: dict[str, LanguageParser] = {}

        for parser in parser_tuple:
            lid = getattr(parser, "language_id", None)
            if not lid or not isinstance(lid, str):
                raise ValueError(
                    f"{type(parser).__name__} has no valid language_id"
                )
            if lid in seen_ids:
                raise ValueError(
                    f"Duplicate language_id '{lid}': "
                    f"{type(seen_ids[lid]).__name__} and {type(parser).__name__}"
                )
            seen_ids[lid] = parser

            extensions = parser.get_extensions()
            for ext in extensions:
                if not ext.startswith("."):
                    raise ValueError(
                        f"Extension '{ext}' from {lid} does not start with '.'"
                    )
                if ext in extension_map:
                    raise ValueError(
                        f"Extension '{ext}' registered by both "
                        f"{extension_map[ext].language_id} and {lid}"
                    )
                extension_map[ext] = parser

        self._parsers = parser_tuple
        self._extension_map = types.MappingProxyType(extension_map)
        self._extensions_by_length = sorted(
            extension_map.keys(), key=len, reverse=True
        )

    def resolve(self, filename: str) -> Optional[LanguageParser]:
        """Return the parser for *filename* using longest-suffix matching."""
        for ext in self._extensions_by_length:
            if filename.endswith(ext):
                return self._extension_map[ext]
        return None

    def all(self) -> tuple:
        """Return all registered parsers as an immutable tuple."""
        return self._parsers
