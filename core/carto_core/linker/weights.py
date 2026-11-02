"""Versioned logistic weights for link scoring (spec 9.3; ADR 0049 item 1).

The score of a candidate pair is ``sigmoid(intercept + sum(coefficient[f] * feature[f]))`` over
the named features :mod:`carto_core.linker.score` derives from :class:`PairFeatures`. v1 ships
:data:`DEFAULT_WEIGHTS`, set by hand so that a clean one-to-one link (containment 0.9, five
hundred matches, a tight lag) scores above 0.9 and a coincidental overlap (containment 0.2,
chance ratio 0.2, random lag) scores below 0.5; ``carto-eval fit-weights`` writes a fitted set
to a file that ``LinkerSettings.weights_file`` points at. Core never imports scikit-learn.

Every feature is in ``[0, 1]`` except ``log_n_match`` (``log1p`` of the matched tokens), so the
coefficients read as "logits per unit" and the sign of each one is the monotonicity the property
tests assert: positive for containment, negative for concentration and chance ratio.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

__all__ = [
    "DEFAULT_WEIGHTS",
    "FEATURE_NAMES",
    "MAX_WEIGHTS_FILE_BYTES",
    "Weights",
    "WeightsError",
    "load_weights",
]

FEATURE_NAMES: Final[tuple[str, ...]] = (
    "c_ab",
    "c_ba",
    "log_n_match",
    "lag_consistency",
    "lag_direction",
    "fanout_penalty",
    "concentration",
    "chance_ratio",
    "pairing_consistency",
)
"""The feature vector :func:`carto_core.linker.score.feature_vector` produces, in order."""

MAX_WEIGHTS_FILE_BYTES: Final = 64 * 1024
"""A weights file is a few hundred bytes; anything larger is not one (spec 2.3 invariant 8)."""


class WeightsError(Exception):
    """A weights file could not be used. The message names the path, never its content."""


class Weights(BaseModel):
    """One versioned set of logistic weights."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=64)
    fitted_on: str | None = Field(
        default=None, max_length=256, description="What the weights were fitted on, or None."
    )
    intercept: float
    coefficients: dict[str, float]

    @model_validator(mode="after")
    def _known_features(self) -> Weights:
        unknown = sorted(set(self.coefficients) - set(FEATURE_NAMES))
        if unknown:
            msg = f"unknown features in weights: {', '.join(unknown)}"
            raise ValueError(msg)
        for value in (self.intercept, *self.coefficients.values()):
            if value != value or value in (float("inf"), float("-inf")):
                msg = "weights must be finite numbers"
                raise ValueError(msg)
        return self

    def coefficient(self, feature: str) -> float:
        return self.coefficients.get(feature, 0.0)


DEFAULT_WEIGHTS: Final = Weights(
    version="v1-hand-set",
    fitted_on=None,
    intercept=-4.0,
    coefficients={
        "c_ab": 3.0,
        "c_ba": 1.5,
        "log_n_match": 0.4,
        "lag_consistency": 1.0,
        "lag_direction": 1.0,
        "fanout_penalty": -2.0,
        "concentration": -3.0,
        "chance_ratio": -6.0,
        "pairing_consistency": 1.0,
    },
)
"""The shipped v1 weights. Worked examples (logit, score):

- clean one-to-one, ``c_ab 0.9, c_ba 0.9, n_match 500``, tight lag, directed, fanout 1,
  concentration 0.02, chance ratio ~0: ``-4 + 2.7 + 1.35 + 2.49 + 1 + 1 - 0.06 = 4.48`` (0.99);
- coincidental, ``c_ab 0.2, c_ba 0.2, n_match 100``, random lag, concentration 0.1, chance
  ratio 0.2: ``-4 + 0.6 + 0.3 + 1.85 + 0 + 0.5 - 0.3 - 1.2 = -2.25`` (0.10).
"""


def load_weights(path: Path | None) -> Weights:
    """The weights at ``path`` (JSON of :class:`Weights`), or :data:`DEFAULT_WEIGHTS` when none
    is configured. The file is size-capped and validated; nothing in it is executed."""
    if path is None:
        return DEFAULT_WEIGHTS
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_WEIGHTS_FILE_BYTES + 1)
    except OSError as exc:
        msg = f"cannot read weights file {path}"
        raise WeightsError(msg) from exc
    if len(data) > MAX_WEIGHTS_FILE_BYTES:
        msg = f"weights file {path} is larger than {MAX_WEIGHTS_FILE_BYTES} bytes"
        raise WeightsError(msg)
    try:
        return Weights.model_validate_json(data)
    except ValidationError as exc:
        msg = f"weights file {path} is not a valid weights document ({exc.error_count()} errors)"
        raise WeightsError(msg) from None
