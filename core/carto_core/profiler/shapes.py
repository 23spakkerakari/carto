"""Domain size from a value shape, for the chance ratio of spec 9.3 (ADR 0048).

``expected chance matches = n_a * n_b / domain_size`` where ``domain_size`` is estimated from
the dominant shape: ``9999`` gives ``10^4`` and ``AA-9999999`` gives ``26^2 x 10^7``. The shape
alphabet is what :func:`carto_schema.forms.shape` emits: ``9`` for a digit, ``A`` for a letter
(case is not distinguished, and the ``norm`` and ``alnum`` forms are lower case anyway, so 26),
any other character stands for itself (one choice), and ``+`` marks a run collapsed after
:data:`~carto_schema.forms.SHAPE_MAX_RUN` characters. A collapsed run is at least one character
longer than shown, so ``+`` counts as one more character of the run it ends; the result is then
a lower bound, which makes the chance ratio an upper bound and the hard filter conservative.
The linker (package C) calls :func:`domain_size`.
"""

from __future__ import annotations

from typing import Final

from carto_schema.forms import shape as shape_of

__all__ = ["ALPHABET_SIZES", "domain_size"]

ALPHABET_SIZES: Final[dict[str, float]] = {"9": 10.0, "A": 26.0}
"""Choices per shape character class; every other character is a separator with one choice."""


def domain_size(shape: str) -> float | None:
    """Choices the shape admits, or ``None`` for an empty string or a string that is not a shape
    (one :func:`carto_schema.forms.shape` does not map to itself)."""
    if not shape or shape_of(shape) != shape:
        return None
    size = 1.0
    previous = ""
    for char in shape:
        if char == "+":
            if previous not in ALPHABET_SIZES:
                return None
            size *= ALPHABET_SIZES[previous]
            continue
        size *= ALPHABET_SIZES.get(char, 1.0)
        previous = char
    return size
