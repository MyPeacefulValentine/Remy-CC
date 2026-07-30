"""Deterministic canonical selection for same-name parsed symbols."""

import re
from dataclasses import dataclass

from parsers.base import SymbolInfo


TYPE_VARIANT = "type_variant"
SIGNATURE_VARIANT = "signature_variant"
DUPLICATE_DEFINITION = "duplicate_definition"


@dataclass(frozen=True)
class SymbolOccurrence:
    symbol: SymbolInfo
    occurrence_index: int
    is_canonical: bool
    conflict_kind: str
    selection_reason: str


@dataclass(frozen=True)
class SymbolSelection:
    canonical_symbols: list
    occurrences: list


def normalize_signature(args):
    return re.sub(r"\s+", " ", args or "").strip()


def _stable_key(symbol):
    lineno = symbol.lineno if symbol.lineno is not None else -1
    end_lineno = symbol.end_lineno if symbol.end_lineno is not None else lineno
    return (
        lineno,
        end_lineno,
        symbol.name or "",
        symbol.type or "",
        normalize_signature(symbol.args),
        symbol.source_segment or "",
        symbol.docstring or "",
        tuple(symbol.bases or ()),
    )


def _classify(symbols):
    if len({symbol.type for symbol in symbols}) > 1:
        return TYPE_VARIANT
    if len({normalize_signature(symbol.args) for symbol in symbols}) > 1:
        return SIGNATURE_VARIANT
    return DUPLICATE_DEFINITION


def _source_extent_key(symbol):
    lineno = symbol.lineno if symbol.lineno is not None else -1
    end_lineno = symbol.end_lineno if symbol.end_lineno is not None else lineno
    span = max(0, end_lineno - lineno)
    segment = symbol.source_segment or ""
    return (
        -bool(segment),
        -span,
        -len(segment),
        _stable_key(symbol),
    )


def select_symbols(symbols):
    """Return one canonical symbol per name and auditable conflict occurrences."""
    groups = {}
    for symbol in symbols:
        groups.setdefault(symbol.name, []).append(symbol)

    canonical = []
    occurrences = []
    for name in sorted(groups):
        ordered = sorted(groups[name], key=_stable_key)
        if len(ordered) == 1:
            canonical.append(ordered[0])
            continue

        conflict_kind = _classify(ordered)
        if conflict_kind == SIGNATURE_VARIANT:
            selected = ordered[0]
            selection_reason = "earliest_source_position"
        else:
            selected = min(ordered, key=_source_extent_key)
            selection_reason = "max_source_extent_then_position"

        canonical.append(selected)
        selected_id = id(selected)
        for index, symbol in enumerate(ordered):
            occurrences.append(SymbolOccurrence(
                symbol=symbol,
                occurrence_index=index,
                is_canonical=id(symbol) == selected_id,
                conflict_kind=conflict_kind,
                selection_reason=selection_reason,
            ))

    canonical.sort(key=_stable_key)
    return SymbolSelection(canonical_symbols=canonical, occurrences=occurrences)
