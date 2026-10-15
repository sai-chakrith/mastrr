# -*- coding: utf-8 -*-
"""
Unit tests for retrieval.py -- in particular the sub-skill scoping this
module exists to add (see its own docstring: a global TF-IDF search across
every notes file let an unrelated topic's chunk outrank the actual target
topic whenever it happened to repeat query words more). These tests run
against the real notes corpus on disk, not a mocked one.
"""
from backend import retrieval


def test_scopes_results_to_the_target_subskills_own_notes_file_when_it_has_enough():
    # percentages.txt has 4 paragraphs -- more than top_k=3, so scoping alone
    # should satisfy the request without any backfill from other files
    results = retrieval.retrieve_top_chunks(
        query="Explain the concept of Percentages with common mistakes",
        top_k=3,
        sub_skill="Percentages",
    )
    assert len(results) == 3
    assert all(chunk_id.startswith("percentages.txt#") for chunk_id, _ in results)


def test_returns_short_topic_context_without_unrelated_backfill():
    # A shortage is useful coverage information, not permission to pad context.
    results = retrieval.retrieve_top_chunks(
        query="Explain Ratio and Proportion",
        top_k=5,
        sub_skill="Ratio & Proportion",
    )
    assert len(results) == 3

    own_file = [cid for cid, _ in results if cid.startswith("ratio_and_proportion.txt#")]
    backfilled = [cid for cid, _ in results if not cid.startswith("ratio_and_proportion.txt#")]
    assert len(own_file) == 3  # all of the sub-skill's own chunks, ranked first
    assert len(backfilled) == 0
    assert len({cid for cid, _ in results}) == 3


def test_missing_topic_notes_returns_no_context():
    results = retrieval.retrieve_top_chunks(
        query="Explain this topic",
        top_k=3,
        sub_skill="Not A Real Sub-Skill With No Notes File",
    )
    assert results == []


def test_normalize_tokens_matches_filenames_that_dont_match_the_subskill_name_verbatim():
    # this is the exact mismatch retrieval.py's docstring calls out:
    # "Ratio & Proportion" vs. its notes file "ratio_and_proportion.txt"
    assert retrieval._normalize_tokens("Ratio & Proportion") == retrieval._normalize_tokens("ratio_and_proportion")
    assert retrieval._normalize_tokens("Profit & Loss") == retrieval._normalize_tokens("profit_loss")


def test_normalize_tokens_drops_the_word_and_so_it_doesnt_cause_false_mismatches():
    # "and" is stripped so "Ratio and Proportion" phrasing and the
    # underscore-joined filename tokenize to the same set
    assert "and" not in retrieval._normalize_tokens("Ratio and Proportion")


def test_unscoped_corpus_search_is_still_available():
    assert len(retrieval.retrieve_top_chunks("percentages", top_k=2)) == 2


def test_query_selects_the_relevant_explanation_within_a_topic():
    results = retrieval.retrieve_top_chunks(
        "successive percentage changes multiply combined effect",
        top_k=1, sub_skill="Percentages",
    )
    assert results[0][0] == "percentages.txt#chunk4"
    assert "multiply rather than add" in results[0][1]


def test_empty_or_negative_top_k_is_rejected():
    import pytest

    for count in (0, -1):
        with pytest.raises(ValueError):
            retrieval.retrieve_top_chunks("percentages", top_k=count)
