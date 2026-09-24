"""Tests for safe JEV configuration and typed choice normalization."""


def test_no_key_means_disabled(add_agent_to_path, monkeypatch):
    import jev
    for name in ("TYPESAFE_API_KEY", "JEV_API_KEY", "TYPESAFE_ENABLED", "JEV_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    assert jev._api_key() == ""
    assert jev.is_enabled() is False


def test_key_auto_enables(add_agent_to_path, monkeypatch):
    import jev
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-key")
    monkeypatch.delenv("TYPESAFE_ENABLED", raising=False)
    monkeypatch.delenv("JEV_ENABLED", raising=False)
    assert jev.is_enabled() is True


def test_explicit_disable_wins(add_agent_to_path, monkeypatch):
    import jev
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-only-key")
    monkeypatch.setenv("TYPESAFE_ENABLED", "false")
    assert jev.is_enabled() is False


def test_choice_normalization(add_agent_to_path):
    import jev
    result = jev._read_choice({
        "decision": {
            "choice": "deny",
            "confidence": 0.81,
            "probabilities": {"allow": 0.05, "deny": 0.81},
        }
    })
    assert result == {
        "decision": "deny",
        "confidence": 0.81,
        "probabilities": {"allow": 0.05, "deny": 0.81},
    }
