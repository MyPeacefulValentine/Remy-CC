"""Entry module for the oracle Python fixture."""

import util


class Derived(util.Base):
    """Overrides the Base greeting via helper arithmetic."""

    def greet(self) -> str:
        total = util.helper(2)
        return f"derived-{total}"


def main():
    derived = Derived()
    return derived.greet()
