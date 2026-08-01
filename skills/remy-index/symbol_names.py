"""Shared symbol-name normalization."""

import re


def tokenize_symbol(name):
    """Split snake_case, camelCase, and namespace separators into space-separated tokens."""
    value = name.replace("_", " ").replace("::", " ")
    value = re.sub(r"([a-z])([A-Z])", r"\1 \2", value)
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", value)
    return re.sub(r"\s+", " ", value).strip()
