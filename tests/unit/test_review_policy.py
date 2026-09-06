from src.product.contracts import EvidenceClaim, SignalDraft
from src.product.review_policy import review_reasons


def signal(**changes):
    values = {
        "signal_id": "s1",
        "material_id": "m1",
        "summary": "summary",
        "claims": (EvidenceClaim("fact", "quote"),),
        "relevance": "relevant",
        "importance": "medium",
        "interest": "PR",
        "impact": "impact",
        "urgency": "routine",
        "confidence": 0.1,
    }
    values.update(changes)
    return SignalDraft(**values)


def test_confidence_alone_does_not_trigger_uncalibrated_policy():
    assert review_reasons(signal()) == ()


def test_observable_risks_trigger_review_with_reasons():
    reasons = review_reasons(
        signal(
            relevance="borderline",
            importance="critical",
            kind="npa_candidate",
            unknowns=("scope",),
        )
    )
    assert reasons == (
        "critical_or_urgent",
        "uncertain_relevance",
        "open_questions",
        "unconfirmed_npa",
    )
