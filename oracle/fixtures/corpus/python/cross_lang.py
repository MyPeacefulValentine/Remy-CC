"""Cross-language collision fixture: bare call whose only same-named
symbol lives in rust/cross_lang.rs. The resolver's language-bounded
global tier must leave this edge unresolved."""


def calls_cross_lang():
    return cross_lang_probe()  # noqa: F821 - deliberately undefined here
