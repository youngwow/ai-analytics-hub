"""src/processing/dedup.py — S1: exact hash, SimHash, then cosine over embeddings."""

from __future__ import annotations

import pytest
from support import ARTICLE_TEXT, NEWS_TEXT

from src.config import ProcessingConfig
from src.processing.dedup import (
    Match,
    centroid,
    cosine,
    decode_vector,
    divergent,
    encode_vector,
    find_match,
    hamming,
    simhash,
    tokens,
)

REPRINT_TEXT = ARTICLE_TEXT.replace("внесло в правительство", "направило в правительство")


def _candidate(item_id: int, **overrides) -> dict:
    """A row shaped like `documents.clustered_candidates()` returns."""
    base = {
        "id": item_id,
        "item_id": item_id,
        "cluster_id": item_id,
        "type": "news",
        "simhash": simhash(NEWS_TEXT),
        "embedding": None,
        "npa_key": None,
    }
    return {**base, **overrides}


# ── tokens / simhash / hamming ─────────────────────────────────────────────


def test_tokens_are_lowercased_word_trigrams():
    assert tokens("Минцифры внесло законопроект № 112233") == [
        "минцифры внесло законопроект",
        "внесло законопроект 112233",
    ]


def test_tokens_of_a_text_shorter_than_the_shingle_are_the_words():
    assert tokens("Минцифры внесло") == ["минцифры", "внесло"]


def test_simhash_is_a_stable_16_character_hex_string():
    value = simhash(ARTICLE_TEXT)
    assert len(value) == 16
    assert int(value, 16) >= 0
    assert simhash(ARTICLE_TEXT) == value


@pytest.mark.parametrize("text", ["", "   ", "\n\n"], ids=["empty", "spaces", "newlines"])
def test_simhash_of_blank_text_is_an_empty_string(text):
    assert simhash(text) == ""


def test_near_identical_texts_stay_within_the_default_distance():
    assert hamming(simhash(ARTICLE_TEXT), simhash(REPRINT_TEXT)) <= ProcessingConfig().simhash_distance


def test_an_identical_reprint_has_distance_zero():
    assert hamming(simhash(ARTICLE_TEXT), simhash(ARTICLE_TEXT + "\n")) == 0


def test_unrelated_texts_are_far_apart():
    assert hamming(simhash(ARTICLE_TEXT), simhash(NEWS_TEXT)) > 3


@pytest.mark.parametrize(
    ("left", "right"),
    [("", "0f1e2d3c4b5a6978"), ("0f1e2d3c4b5a6978", ""), ("", ""), ("не-хеш", "0f1e2d3c4b5a6978")],
    ids=["empty-left", "empty-right", "both-empty", "not-hex"],
)
def test_hamming_treats_an_unusable_hash_as_infinitely_far(left, right):
    assert hamming(left, right) == 65


# ── vectors ────────────────────────────────────────────────────────────────


def test_encode_decode_round_trip():
    values = [0.5, -0.25, 0.125, 0.0]
    assert decode_vector(encode_vector(values)) == pytest.approx(values)


def test_encoded_vectors_are_four_bytes_per_value():
    assert len(encode_vector([1.0] * 768)) == 768 * 4


@pytest.mark.parametrize("blob", [None, b""], ids=["none", "empty"])
def test_decode_of_a_missing_blob_is_an_empty_vector(blob):
    assert decode_vector(blob) == []


def test_decode_of_a_truncated_blob_is_an_empty_vector():
    assert decode_vector(encode_vector([1.0, 2.0])[:5]) == []


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ([1.0, 0.0], [1.0, 0.0], 1.0),
        ([1.0, 0.0], [0.0, 1.0], 0.0),
        ([1.0, 0.0], [-1.0, 0.0], -1.0),
        ([1.0, 1.0], [2.0, 2.0], 1.0),
        ([1.0, 0.0], [], 0.0),
        ([], [1.0, 0.0], 0.0),
        ([1.0, 0.0], [1.0, 0.0, 0.0], 0.0),
        ([0.0, 0.0], [1.0, 0.0], 0.0),
    ],
    ids=["identical", "orthogonal", "opposite", "same-direction", "empty-right", "empty-left",
         "length-mismatch", "zero-vector"],
)
def test_cosine(left, right, expected):
    assert cosine(left, right) == pytest.approx(expected)


def test_centroid_averages_component_wise():
    assert centroid([[1.0, 0.0], [0.0, 1.0], [2.0, 2.0]]) == pytest.approx([1.0, 1.0])


def test_centroid_of_nothing_is_empty():
    assert centroid([]) == []
    assert centroid([[], []]) == []


def test_centroid_of_mismatched_widths_falls_back_to_the_first_vector():
    assert centroid([[1.0, 2.0], [3.0]]) == [1.0, 2.0]


# ── divergent opinions ─────────────────────────────────────────────────────


def test_two_texts_that_both_voice_a_position_are_divergent():
    assert divergent(
        "Глава ассоциации заявил, что требование избыточно.",
        "Представитель министерства подчеркнул необходимость аккредитации.",
    ) is True


def test_a_plain_reprint_is_not_divergent():
    assert divergent(ARTICLE_TEXT, REPRINT_TEXT) is False


def test_one_opinion_against_a_factual_report_is_not_divergent():
    assert divergent("Депутат раскритиковал законопроект.", ARTICLE_TEXT) is False


# ── find_match ─────────────────────────────────────────────────────────────


def test_find_match_returns_none_without_candidates():
    assert find_match([], text_simhash=simhash(ARTICLE_TEXT)) is None


def test_find_match_joins_a_near_duplicate_by_simhash():
    candidate = _candidate(7, simhash=simhash(ARTICLE_TEXT))
    match = find_match([candidate], text_simhash=simhash(REPRINT_TEXT))
    assert isinstance(match, Match)
    assert (match.item_id, match.cluster_id) == (7, 7)
    assert match.reason.startswith("simhash d=")
    assert match.score > 0.9


def test_find_match_ignores_a_distant_simhash_without_embeddings():
    candidate = _candidate(7, simhash=simhash(NEWS_TEXT))
    assert find_match([candidate], text_simhash=simhash(ARTICLE_TEXT)) is None


def test_find_match_joins_by_cosine_when_the_simhash_is_far():
    candidate = _candidate(
        7, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0, 0.0])
    )
    match = find_match(
        [candidate],
        text_simhash=simhash(ARTICLE_TEXT),
        embedding=[0.95, 0.1, 0.0],
        threshold=0.86,
    )
    assert match is not None
    assert (match.item_id, match.cluster_id) == (7, 7)
    assert match.reason.startswith("cosine ")
    assert match.score == pytest.approx(0.99450, abs=1e-4)


def test_find_match_ignores_a_cosine_below_the_threshold():
    candidate = _candidate(
        7, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0, 0.0])
    )
    match = find_match(
        [candidate],
        text_simhash=simhash(ARTICLE_TEXT),
        embedding=[0.5, 0.9, 0.0],
        threshold=0.86,
    )
    assert match is None


def test_find_match_never_joins_an_npa_card_even_on_an_identical_text():
    """An act's identity is its number; the service decides, not SimHash."""
    same = simhash(ARTICLE_TEXT)
    candidate = _candidate(7, type="npa", npa_key="112233-8", simhash=same,
                           embedding=encode_vector([1.0, 0.0, 0.0]))
    assert find_match([candidate], text_simhash=same, embedding=[1.0, 0.0, 0.0]) is None


def test_find_match_skips_npa_rows_but_still_sees_the_news_ones():
    same = simhash(ARTICLE_TEXT)
    rows = [_candidate(7, type="npa", simhash=same), _candidate(9, simhash=same)]
    match = find_match(rows, text_simhash=same)
    assert match.item_id == 9


def test_find_match_prefers_the_closest_candidate():
    rows = [
        _candidate(1, simhash=simhash(REPRINT_TEXT)),
        _candidate(2, simhash=simhash(ARTICLE_TEXT)),
    ]
    match = find_match(rows, text_simhash=simhash(ARTICLE_TEXT))
    assert (match.item_id, match.score) == (2, 1.0)


def test_find_match_honours_a_stricter_max_distance():
    candidate = _candidate(7, simhash=simhash(ARTICLE_TEXT))
    assert find_match([candidate], text_simhash=simhash(REPRINT_TEXT), max_distance=0) is None


def test_find_match_skips_a_candidate_without_a_stored_embedding():
    candidate = _candidate(7, simhash=simhash(NEWS_TEXT), embedding=None)
    assert find_match([candidate], text_simhash=simhash(ARTICLE_TEXT), embedding=[1.0, 0.0]) is None
