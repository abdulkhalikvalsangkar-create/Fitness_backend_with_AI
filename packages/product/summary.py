"""App-facing summary of a product analysis.

The scan response carries the full block list for rendering, but the app also
needs a few fields it should not have to dig out of blocks or guess at: who the
product is, how it was identified, and a single number for the score row.
Everything here is derived deterministically from the analysis — no model call.
"""

from __future__ import annotations

from typing import Any, Optional

from packages.domain.enums import HazardLevel, Severity, Verdict
from packages.domain.models import ProductAnalysis

# Points removed from 100 per finding. Ingredient lists are long and most
# entries are benign, so a handful of findings must move the score a lot while
# one low-level finding barely moves it.
_HAZARD_PENALTY = {
    HazardLevel.NONE: 0,
    HazardLevel.LOW: 2,
    HazardLevel.MODERATE: 8,
    HazardLevel.HIGH: 20,
    HazardLevel.UNKNOWN: 0,
}
_PERSONAL_PENALTY = {
    Severity.INFO: 0,
    Severity.LOW: 3,
    Severity.MODERATE: 10,
    Severity.HIGH: 20,
    Severity.CRITICAL: 35,
}

# The score may never contradict the verdict the rules engine reached: a
# "Not recommended" product cannot read as 85/100 because it had few findings.
_VERDICT_CEILING = {
    Verdict.GENERALLY_SUITABLE: 100,
    Verdict.USE_WITH_CAUTION: 69,
    Verdict.NOT_RECOMMENDED: 39,
}
_VERDICT_FLOOR = {
    Verdict.GENERALLY_SUITABLE: 70,
    Verdict.USE_WITH_CAUTION: 40,
    Verdict.NOT_RECOMMENDED: 0,
}


def product_score(analysis: ProductAnalysis) -> Optional[dict[str, Any]]:
    """0-100 score and a label, or None when there is not enough data.

    `label` is the rules engine's verdict text, so the number and the words
    always agree. Returns None for "Insufficient data" rather than inventing a
    number for a product the engine could not assess.
    """
    verdict = analysis.verdict
    if verdict not in _VERDICT_CEILING:
        return None

    value = 100
    for hazard in analysis.hazards:
        value -= _HAZARD_PENALTY.get(hazard.hazard_level, 0)
        if hazard.banned_in:
            value -= 10
        if hazard.endocrine_flag:
            value -= 5
    for flag in analysis.personal_flags:
        value -= _PERSONAL_PENALTY.get(flag.severity, 0)

    value = max(_VERDICT_FLOOR[verdict], min(_VERDICT_CEILING[verdict], value))
    return {"value": int(value), "scale_max": 100, "label": str(verdict.value)}


def product_summary(analysis: Optional[ProductAnalysis]) -> dict[str, Any]:
    """`product`, `identified_via` and `score` for the scan response.

    `product.barcode` is the decoded code whenever one was read, independent of
    how the ingredients were obtained — a decoded barcode with
    `identified_via: "ocr"` means the code was read but no database record had
    an ingredient panel, so the label was read instead.
    """
    if analysis is None:
        return {"product": None, "identified_via": None, "score": None}

    identity = analysis.identity
    product = {
        "name": identity.name if identity else None,
        "brand": identity.brand if identity else None,
        "quantity": identity.quantity if identity else None,
        "barcode": identity.barcode if identity else None,
        "image_url": identity.image_url if identity else None,
    }
    return {
        "product": product,
        "identified_via": analysis.identified_via,
        "score": product_score(analysis),
    }
