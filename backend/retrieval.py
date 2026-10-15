# -*- coding: utf-8 -*-
"""
retrieval.py -- chunking + retrieval over knowledge/notes/, factored out
of run_pipeline.py (which prototyped this inline) into something the API
can call per-request.

run_pipeline.py's prototype ran a single global TF-IDF search across
every note file, keyed only by a query string mentioning the sub-skill
name. In practice that lets a chunk from an unrelated topic outrank the
target topic's own notes whenever it happens to repeat the query words
more (e.g. a "Profit & Loss" paragraph that opens by explaining it
depends on Percentages can outscore a genuine Percentages paragraph for
the query "Explain Percentages..."). But Track 2's own plan is explicit
that retrieval should be scoped to "that sub-skill" -- so this version
restricts ranking to the target sub-skill's own notes file. Missing or
short topic material is reported honestly instead of padded with unrelated
content. Unscoped searches remain available for general corpus exploration.

File-to-sub_skill matching is done by token set rather than a hardcoded
slug rule, because the notes filenames aren't perfectly consistent
(e.g. "profit_loss.txt" for "Profit & Loss", vs. "ratio_and_proportion.txt"
for "Ratio & Proportion") -- token-set matching handles both without
special-casing either.
"""
import glob
import math
import os
import re
from collections import Counter
from pathlib import Path

NOTES_DIR = Path(__file__).resolve().parent.parent / "knowledge" / "notes"


def _normalize_tokens(s: str) -> frozenset:
    s = s.lower().replace("&", " ")
    tokens = re.split(r"[^a-z0-9]+", s)
    return frozenset(t for t in tokens if t and t != "and")


def load_chunks(notes_folder=NOTES_DIR):
    """Returns (chunks, chunk_ids, file_of_chunk) where file_of_chunk[i] is the
    notes filename stem that chunk i came from."""
    chunks, chunk_ids, file_of_chunk = [], [], []
    for filepath in sorted(glob.glob(os.path.join(notes_folder, "*.txt"))):
        stem = os.path.splitext(os.path.basename(filepath))[0]
        with open(filepath, encoding="utf-8") as f:
            paragraphs = [p.strip() for p in f.read().split("\n\n") if p.strip()]
        for i, para in enumerate(paragraphs):
            chunks.append(para)
            chunk_ids.append(f"{stem}.txt#chunk{i + 1}")
            file_of_chunk.append(stem)
    return chunks, chunk_ids, file_of_chunk


def _build_file_token_index(file_of_chunk):
    stems = sorted(set(file_of_chunk))
    return {_normalize_tokens(stem): stem for stem in stems}


# Loaded once per process -- the notes corpus doesn't change at request time.
_CHUNKS, _CHUNK_IDS, _FILE_OF_CHUNK = load_chunks()
_FILE_TOKEN_INDEX = _build_file_token_index(_FILE_OF_CHUNK)
# The small paragraph corpus does not need a native scientific runtime.
# Smoothed TF-IDF weights and cosine normalization, implemented directly.
_STOP_WORDS = frozenset("a an and are as at be by for from in is it of on or that the this to with explain concept common mistakes examples".split())


def _terms(text):
    return [t for t in re.findall(r"\b\w\w+\b", text.lower()) if t not in _STOP_WORDS]


_DOCUMENT_TERMS = [Counter(_terms(chunk)) for chunk in _CHUNKS]
_DOCUMENT_FREQUENCIES = Counter(term for counts in _DOCUMENT_TERMS for term in counts)
_IDF = {term: math.log((1 + len(_CHUNKS)) / (1 + frequency)) + 1
        for term, frequency in _DOCUMENT_FREQUENCIES.items()}


def _vector(text):
    weights = {term: count * _IDF[term] for term, count in Counter(_terms(text)).items() if term in _IDF}
    norm = math.sqrt(sum(w * w for w in weights.values()))
    return {term: weight / norm for term, weight in weights.items()} if norm else {}


_CHUNK_VECTORS = [_vector(chunk) for chunk in _CHUNKS]


def _indices_for_sub_skill(sub_skill: str):
    stem = _FILE_TOKEN_INDEX.get(_normalize_tokens(sub_skill))
    if stem is None:
        return []
    return [i for i, f in enumerate(_FILE_OF_CHUNK) if f == stem]


def _rank(query: str, candidate_indices) -> list:
    if not candidate_indices:
        return []
    query_vector = _vector(query)
    scores = {i: sum(weight * _CHUNK_VECTORS[i].get(term, 0)
                     for term, weight in query_vector.items()) for i in candidate_indices}
    return sorted(scores, key=lambda i: scores[i], reverse=True)


def retrieve_top_chunks(query: str, top_k: int = 3, sub_skill: str = None):
    """Returns a list of (chunk_id, chunk_text) tuples, most relevant first.
    An explicit topic never falls back to other topics. A short result is
    useful evidence of missing coverage, not a reason to pad the context.
    """
    if top_k < 1:
        raise ValueError("top_k must be positive")
    if not _CHUNK_VECTORS:
        return []
    candidates = _indices_for_sub_skill(sub_skill) if sub_skill is not None else range(len(_CHUNKS))
    ranked_indices = _rank(query, candidates)

    top = ranked_indices[:top_k]
    return [(_CHUNK_IDS[i], _CHUNKS[i]) for i in top]
