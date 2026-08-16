"""Edge cases stressing ast.unparse argument normalization."""

import functools

from util import helper as doubled


@functools.lru_cache(maxsize=None)
def normalized(a=0x10, b=1_000, c=1e3, d=.5, e='single', f="double"):
    """Constants are re-serialized from their parsed values."""
    return doubled(a + b)


async def keywords(pos_only, /, plain, *args, kw_only: int = 1 + 2, **rest):
    return plain


def expressions(x=[1, 2], y={'k': (1,)}, z=lambda v: v + 1, w=f"v={1 + 1}!"):
    def nested_not_a_symbol():
        return inner_call(x)

    return nested_not_a_symbol()


class Outer:
    """One class level of methods becomes symbols."""

    class Inner:
        def hidden(self):
            return 1

    def method(self, scale: float = 2.0) -> float:
        value = self.compute(scale)  # attribute call form
        return value


def observers(bus):
    for callback in bus.listeners:
        callback()
    bus.listeners.append(observers)
