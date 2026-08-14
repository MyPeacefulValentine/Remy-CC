"""Utility layer for the oracle Python fixture."""


class Base:
    """Root of the fixture inheritance chain."""

    def greet(self) -> str:
        return "base"


def helper(value):
    """Double the given value."""
    return value * 2
