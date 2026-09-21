from dive_memory.gate import GateAction, _sensitive, decide


def test_write_gate_exposes_versioned_features_and_reason_codes():
    decision = decide("请记住我喜欢绿茶", explicit=True)

    assert decision.action is GateAction.WRITE
    assert decision.accepted
    assert decision.policy_version == "utility-baseline-v1"
    assert decision.reason_code == "UTILITY_ABOVE_WRITE_THRESHOLD"
    assert set(decision.features) >= {
        "future_usefulness", "durability", "specificity", "novelty",
        "importance", "source_confidence", "redundancy", "sensitivity_risk",
        "temporal_relevance", "explicit_remember_request",
    }


def test_write_gate_has_policy_skip_and_review_band():
    sensitive = decide("remember my API key is abc", explicit=True)
    assert sensitive.action is GateAction.SKIP
    assert sensitive.reason_code == "SENSITIVE_DATA"

    review = decide("I will probably keep using this workflow", source_reliability=0.55)
    assert review.action in {GateAction.REVIEW, GateAction.SKIP}
    assert not review.accepted


def test_sensitive_detection_catches_unlabelled_secret_shapes():
    """Value-shaped secrets must be caught even without a label word.

    The semantic-gate adversarial probe found a bare card number passing the
    label-only patterns, so these shapes are now detected directly.
    """
    for text in (
        "my credit card number is 4111 1111 1111 1111",
        "card: 4111111111111111",
        "my credit card number is 4111-1111-1111-1111",
        "身份证 11010119900307123X",
        "my token is abcdefghijklmnopqrst.uvw",
        "remember my API key is sk-live-abc123",
    ):
        assert _sensitive(text), text


def test_sensitive_detection_does_not_flag_ordinary_memory_text():
    """Detectors must not eat legitimate durable facts or dates."""
    for text in (
        "I have been learning Spanish for two years.",
        "My birthday is in early March.",
        "I prefer dark mode in my editor.",
        "I always book flights at least three weeks ahead.",
        "I graduated with a degree in Business Administration.",
        "Call me at 555-1234.",
        "The meeting is on 2026-09-21.",
    ):
        assert not _sensitive(text), text
