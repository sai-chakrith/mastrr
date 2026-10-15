# -*- coding: utf-8 -*-
"""
Unit tests for tutor.py: the LLM vs. extractive-fallback branching in
generate_tutoring_explanation().

The Anthropic client is the one thing in this project that's legitimate to
mock -- it's a paid external network call, not project logic -- so these
tests stub anthropic.Anthropic rather than hitting the real API. Everything
else (retrieval over the real notes corpus) runs for real, same as the rest
of the suite.
"""
import anthropic

from backend import tutor

REAL_SUB_SKILL = "Percentages"  # has real notes on disk, so retrieval returns chunks


class _FakeTextBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, response=None, exc=None):
        self._response = response
        self._exc = exc
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._exc is not None:
            raise self._exc
        return self._response


class _FakeAnthropicClient:
    def __init__(self, response=None, exc=None):
        self.messages = _FakeMessages(response=response, exc=exc)


def test_falls_back_to_extractive_when_no_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = tutor.generate_tutoring_explanation(REAL_SUB_SKILL)

    assert result["generated_by"] == "extractive_fallback"
    assert len(result["source_chunks"]) > 0
    assert result["explanation"]  # non-empty: the top chunk's own text
    assert "llm_error" not in result  # no LLM was even attempted


def test_uses_llm_when_api_key_present_and_call_succeeds(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    fake_client = _FakeAnthropicClient(response=_FakeResponse("A grounded explanation of percentages."))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: fake_client)

    result = tutor.generate_tutoring_explanation(REAL_SUB_SKILL)

    assert result["generated_by"] == "llm"
    assert result["explanation"] == "A grounded explanation of percentages."
    assert len(fake_client.messages.calls) == 1
    # the sub-skill and retrieved notes actually made it into the prompt
    assert REAL_SUB_SKILL in fake_client.messages.calls[0]["messages"][0]["content"]


def test_falls_back_to_extractive_when_llm_call_raises(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    fake_client = _FakeAnthropicClient(exc=RuntimeError("simulated network failure"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: fake_client)

    result = tutor.generate_tutoring_explanation(REAL_SUB_SKILL)

    assert result["generated_by"] == "extractive_fallback"
    assert result["llm_error"] == "RuntimeError"
    assert len(result["source_chunks"]) > 0


def test_falls_back_to_extractive_when_llm_returns_empty_text(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    fake_client = _FakeAnthropicClient(response=_FakeResponse(""))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: fake_client)

    result = tutor.generate_tutoring_explanation(REAL_SUB_SKILL)

    assert result["generated_by"] == "extractive_fallback"
    assert result["llm_error"] == "ValueError"


def test_extractive_fallback_with_no_chunks_returns_friendly_message():
    assert tutor._extractive_fallback([]) == "No reference notes were found for this sub-skill yet."


def test_extractive_fallback_returns_top_chunk_text_verbatim():
    chunks = [("some_id#chunk1", "the most relevant paragraph"), ("other_id#chunk2", "a less relevant one")]
    assert tutor._extractive_fallback(chunks) == "the most relevant paragraph"


def test_missing_notes_never_calls_model_or_invents_example(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    result = tutor.generate_tutoring_explanation("Not A Real Topic")
    assert result["coverage"] == "missing"
    assert result["source_chunks"] == []
    assert result["worked_example"] is None
    assert result["fallback_reason"] == "missing_notes"


def test_short_notes_are_reported_without_unrelated_sources(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = tutor.generate_tutoring_explanation("Ratio & Proportion", top_k=5)
    assert result["coverage"] == "limited"
    assert all(c.startswith("ratio_and_proportion.txt#") for c in result["source_chunks"])


def test_personalized_prompt_contains_observations_not_learner_identity(client, monkeypatch):
    from conftest import run_diagnostic
    from backend.database import get_cursor

    def lookup(qid):
        with get_cursor() as cur:
            return cur.execute("SELECT correct_answer FROM questions WHERE id=?", (qid,)).fetchone()[0]

    run_diagnostic(client, lookup, "private_learner", "Percentages", want_correct=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    fake_client = _FakeAnthropicClient(response=_FakeResponse("Teaching based on the supplied notes."))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: fake_client)
    result = tutor.generate_tutoring_explanation("Percentages", learner_id="private_learner")
    prompt = fake_client.messages.calls[0]["messages"][0]["content"]
    assert "private_learner" not in prompt
    assert "observed_errors" in prompt
    assert result["learner_context"]["observed_errors"]
    assert result["learner_context"]["observed_errors"][0]["selected_answer"] is None
    assert result["diagnosis"]["misconception"] is None
    assert any("Legacy diagnostic" in s for s in result["limitations"])


def test_provider_error_details_are_not_returned(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
    fake_client = _FakeAnthropicClient(exc=RuntimeError("secret_provider_payload"))
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kwargs: fake_client)
    result = tutor.generate_tutoring_explanation("Percentages")
    assert "secret_provider_payload" not in str(result)
    assert result["fallback_reason"] == "generation_failed"
