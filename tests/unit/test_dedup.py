"""src/processing/dedup.py — S1: exact hash, SimHash, then cosine over embeddings."""

from __future__ import annotations

import numpy as np
import pytest
from support import ARTICLE_TEXT, NEWS_TEXT

from src.config import ProcessingConfig
from src.models import RawDocument, Source
from src.processing import dedup
from src.processing.dedup import (
    Candidate,
    CandidatePool,
    Match,
    _dominant_width,
    centroid,
    cosine,
    cosine_prepared,
    decode_vector,
    divergent,
    encode_vector,
    find_match,
    hamming,
    pool,
    prepare,
    simhash,
    tokens,
    vector_norm,
)
from src.services.processing_service import ProcessingService, match_text

REPRINT_TEXT = ARTICLE_TEXT.replace("внесло в правительство", "направило в правительство")
NOW = "2026-09-02T12:00:00+00:00"


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


# ── подготовленный пул кандидатов (план 4.3) ───────────────────────────────


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([1.0, 0.0], [1.0, 0.0]),
        ([1.0, 0.0], [0.0, 1.0]),
        ([1.0, 0.0], [-1.0, 0.0]),
        ([1.0, 1.0], [2.0, 2.0]),
        ([0.3, 0.4, 0.5], [0.1, 0.9, 0.2]),
    ],
    ids=["identical", "orthogonal", "opposite", "same-direction", "arbitrary"],
)
def test_cosine_prepared_is_the_same_number_as_cosine(left, right):
    """Предподсчёт норм — оптимизация, а не другая метрика."""
    prepared = cosine_prepared(left, vector_norm(left), right, vector_norm(right))

    assert prepared == pytest.approx(cosine(left, right))


@pytest.mark.parametrize(
    ("left", "left_norm", "right", "right_norm"),
    [
        ([], 0.0, [1.0, 0.0], 1.0),
        ([1.0, 0.0], 1.0, [], 0.0),
        ([1.0, 0.0], 1.0, [1.0, 0.0, 0.0], 1.0),
        ([0.0, 0.0], 0.0, [1.0, 0.0], 1.0),
    ],
    ids=["empty-left", "empty-right", "length-mismatch", "zero-norm"],
)
def test_cosine_prepared_has_no_opinion_about_unusable_vectors(left, left_norm, right, right_norm):
    assert cosine_prepared(left, left_norm, right, right_norm) == 0.0


@pytest.mark.parametrize(
    ("vector", "norm"),
    [([3.0, 4.0], 5.0), ([1.0], 1.0), ([], 0.0), ([0.0, 0.0], 0.0)],
    ids=["pythagoras", "unit", "empty", "zeros"],
)
def test_vector_norm(vector, norm):
    assert vector_norm(vector) == pytest.approx(norm)


def test_a_candidate_carries_the_decoded_vector_and_its_norm():
    row = _candidate(7, embedding=encode_vector([3.0, 4.0]), title="Заголовок")

    candidate = Candidate.from_row(row)

    assert (candidate.item_id, candidate.cluster_id, candidate.type) == (7, 7, "news")
    assert candidate.embedding == pytest.approx([3.0, 4.0])
    assert candidate.norm == pytest.approx(5.0)
    assert (candidate.title, candidate.document_id) == ("Заголовок", 7)


def test_a_candidate_without_an_embedding_has_an_empty_vector_and_a_zero_norm():
    candidate = Candidate.from_row(_candidate(7, embedding=None, simhash=""))

    assert (candidate.embedding, candidate.norm, candidate.simhash) == ([], 0.0, "")


def test_prepare_leaves_an_already_prepared_list_alone():
    prepared = prepare([_candidate(7), _candidate(9)])

    again = prepare(prepared)

    assert again == prepared
    assert all(a is b for a, b in zip(again, prepared))


def test_prepare_unwraps_a_pool_into_its_candidates():
    built = pool([_candidate(7), _candidate(9)])

    assert prepare(built) == built.candidates
    assert [c.item_id for c in prepare(built)] == [7, 9]


def test_prepare_decodes_every_embedding_exactly_once(monkeypatch):
    """Иначе на 2000 кандидатов × 200 документов приходится 400 тысяч раскодирований."""
    calls: list[bytes | None] = []
    original = dedup.decode_vector
    monkeypatch.setattr(
        dedup, "decode_vector", lambda blob: calls.append(blob) or original(blob)
    )
    rows = [_candidate(i, embedding=encode_vector([1.0, 0.0])) for i in range(1, 4)]

    built = pool(rows)
    for _ in range(5):  # пул переиспользуется всеми документами прогона
        find_match(built, text_simhash=simhash(ARTICLE_TEXT), embedding=[1.0, 0.0])

    assert len(calls) == 3


# ── CandidatePool: одна матрица на прогон, одно умножение на документ ──────


def _pool_of(*vectors, **overrides) -> CandidatePool:
    """Пул из кандидатов с заданными векторами; `None` — кандидат без вектора."""
    return pool(
        [
            _candidate(
                index,
                simhash=simhash(NEWS_TEXT),
                embedding=None if vector is None else encode_vector(vector),
                **overrides,
            )
            for index, vector in enumerate(vectors, start=1)
        ]
    )


def test_a_pool_keeps_every_candidate_in_the_given_order():
    built = _pool_of([1.0, 0.0], None, [0.0, 1.0])

    assert len(built) == 3
    assert [c.item_id for c in built] == [1, 2, 3]
    assert built.candidates[1].embedding == []


def test_pool_is_idempotent():
    built = _pool_of([1.0, 0.0])

    assert pool(built) is built


def test_pool_accepts_raw_rows_and_a_prepared_list_alike():
    rows = [_candidate(7, embedding=encode_vector([1.0, 0.0]))]

    from_rows = pool(rows)
    from_prepared = pool(prepare(rows))

    assert [c.item_id for c in from_rows] == [c.item_id for c in from_prepared] == [7]
    assert from_rows.scores([1.0, 0.0]) == pytest.approx(from_prepared.scores([1.0, 0.0]))


def test_scores_are_the_cosines_to_every_candidate_at_its_own_position():
    built = _pool_of([1.0, 0.0], [0.0, 1.0], [1.0, 1.0])

    scores = built.scores([1.0, 0.0])

    assert scores == pytest.approx([1.0, 0.0, cosine([1.0, 0.0], [1.0, 1.0])])
    assert len(scores) == len(built)


def test_a_candidate_without_a_vector_scores_zero_and_keeps_its_place():
    """Матрица собирается только из векторных кандидатов — позиции обязаны совпасть."""
    built = _pool_of([0.0, 1.0], None, [1.0, 0.0])

    scores = built.scores([1.0, 0.0])

    assert scores == pytest.approx([0.0, 0.0, 1.0])


def test_a_candidate_with_a_zero_vector_scores_zero_without_dividing_by_it():
    built = _pool_of([0.0, 0.0], [1.0, 0.0])

    scores = built.scores([1.0, 0.0])

    assert scores == pytest.approx([0.0, 1.0])
    assert np.isfinite(scores).all()


@pytest.mark.parametrize(
    ("vectors", "query"),
    [
        (([1.0, 0.0],), None),
        (([1.0, 0.0],), []),
        (([1.0, 0.0],), [1.0, 0.0, 0.0]),
        (([1.0, 0.0],), [0.0, 0.0]),
        ((None,), [1.0, 0.0]),
        ((), [1.0, 0.0]),
    ],
    ids=["no-query", "empty-query", "dimension-mismatch", "zero-query", "no-vectors",
         "empty-pool"],
)
def test_there_are_no_scores_when_there_is_nothing_to_multiply(vectors, query):
    assert _pool_of(*vectors).scores(query) is None


# ── векторы прежней модели: разные размерности в одной базе ───────────────


def _widths(*widths) -> list[Candidate]:
    """Кандидаты с векторами заданной длины (0 — кандидат вовсе без вектора)."""
    return prepare(
        [
            _candidate(index, embedding=encode_vector([1.0] * width))
            for index, width in enumerate(widths, start=1)
        ]
    )


@pytest.mark.parametrize(
    ("widths", "dominant"),
    [
        ((), 0),
        ((768,), 768),
        ((2, 2, 3), 2),
        ((2, 3, 3), 3),
        ((2, 3), 3),
        ((3, 2), 3),
    ],
    ids=["nothing", "single", "majority-small", "majority-large", "tie", "tie-reversed"],
)
def test_the_dominant_width_is_the_most_common_one_and_ties_go_to_the_larger(widths, dominant):
    assert _dominant_width(_widths(*widths)) == dominant


def test_a_pool_of_mixed_widths_builds_instead_of_raising():
    """Вектор от прежней модели не должен ронять весь прогон на сборке матрицы."""
    built = pool(_widths(2, 2, 3))

    assert len(built) == 3
    assert [len(c.embedding) for c in built] == [2, 2, 3]


def test_a_candidate_of_the_minority_width_scores_zero():
    built = pool(_widths(2, 2, 3))

    scores = built.scores([1.0, 0.0])

    assert scores == pytest.approx([cosine([1.0, 0.0], [1.0, 1.0])] * 2 + [0.0])


def test_the_majority_decides_which_width_gets_the_matrix():
    built = pool(_widths(2, 3, 3))

    scores = built.scores([1.0, 1.0, 0.0])

    assert scores[0] == 0.0
    assert scores[1:] == pytest.approx([cosine([1.0, 1.0, 0.0], [1.0, 1.0, 1.0])] * 2)


def test_a_query_of_the_minority_width_gets_nothing():
    built = pool(_widths(2, 2, 3))

    assert built.scores([1.0, 1.0, 1.0]) is None


def test_a_stale_vector_does_not_disturb_the_scores_of_its_neighbours():
    """Три вектора текущей модели и один от прежней: позиции не должны съехать."""
    rows = [
        _candidate(1, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0, 0.0, 0.0])),
        _candidate(2, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 1.0])),
        _candidate(3, simhash=simhash(NEWS_TEXT), embedding=encode_vector([0.0, 1.0, 0.0, 0.0])),
        _candidate(4, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 1.0, 0.0, 0.0])),
    ]
    query = [1.0, 0.0, 0.0, 0.0]
    built = pool(rows)

    scores = built.scores(query)

    assert scores == pytest.approx([1.0, 0.0, 0.0, cosine(query, [1.0, 1.0, 0.0, 0.0])])
    match = find_match(built, text_simhash=simhash(ARTICLE_TEXT), embedding=query, threshold=0.5)
    assert match.item_id == 1


def test_a_candidate_of_another_width_is_never_matched():
    """Раньше косинус разноразмерных векторов давал 0.0 — поведение то же."""
    stale = _candidate(1, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0, 0.0]))
    current = [
        _candidate(i, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0]))
        for i in (2, 3)
    ]

    match = find_match(
        pool([stale, *current]),
        text_simhash=simhash(ARTICLE_TEXT),
        embedding=[1.0, 0.0],
        threshold=0.5,
    )

    assert match.item_id == 2  # первый кандидат подходящей размерности
    assert find_match(
        pool([stale]), text_simhash=simhash(ARTICLE_TEXT), embedding=[1.0, 0.0, 0.0],
        threshold=0.5,
    ).item_id == 1


def test_the_first_candidate_wins_a_tie_on_equal_scores():
    """Сравнение строгое (`>`): при равенстве побеждает тот, кто в пуле раньше."""
    rows = [
        _candidate(i, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0]))
        for i in (5, 6, 7)
    ]

    match = find_match(
        pool(rows), text_simhash=simhash(ARTICLE_TEXT), embedding=[1.0, 0.0], threshold=0.5
    )

    assert (match.item_id, match.score) == (5, pytest.approx(1.0))


def test_the_pool_scores_the_whole_batch_in_one_call(monkeypatch):
    """Один matvec на документ: цена прогона держится именно на этом."""
    built = _pool_of(*([[1.0, 0.0]] * 5))
    calls: list[list[float]] = []
    original = CandidatePool.scores
    monkeypatch.setattr(
        CandidatePool,
        "scores",
        lambda self, embedding: calls.append(list(embedding or [])) or original(self, embedding),
    )

    find_match(built, text_simhash=simhash(ARTICLE_TEXT), embedding=[1.0, 0.0])

    assert calls == [[1.0, 0.0]]


def test_the_query_is_normalised_once_per_find_match(monkeypatch):
    """Нормы кандидатов посчитаны при сборке пула; на документ остаётся один корень."""
    built = _pool_of(*([[1.0, 0.0]] * 5))
    shapes: list[tuple] = []
    original = np.linalg.norm
    monkeypatch.setattr(
        np.linalg, "norm", lambda a, *args, **kw: shapes.append(np.shape(a)) or original(a, *args, **kw)
    )

    find_match(built, text_simhash=simhash(ARTICLE_TEXT), embedding=[1.0, 0.0])

    assert shapes == [(2,)]


@pytest.mark.parametrize(
    ("rows", "kwargs"),
    [
        ([_candidate(7, simhash=simhash(ARTICLE_TEXT))], {"text_simhash": simhash(REPRINT_TEXT)}),
        (
            [_candidate(7, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0, 0.0]))],
            {"text_simhash": simhash(ARTICLE_TEXT), "embedding": [0.95, 0.1, 0.0]},
        ),
        (
            [_candidate(7, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0, 0.0]))],
            {"text_simhash": simhash(ARTICLE_TEXT), "embedding": [0.5, 0.9, 0.0]},
        ),
        (
            [_candidate(7, type="npa", simhash=simhash(ARTICLE_TEXT))],
            {"text_simhash": simhash(ARTICLE_TEXT)},
        ),
        (
            [_candidate(1, simhash=simhash(REPRINT_TEXT)), _candidate(2, simhash=simhash(ARTICLE_TEXT))],
            {"text_simhash": simhash(ARTICLE_TEXT)},
        ),
    ],
    ids=["simhash-hit", "cosine-above-threshold", "cosine-below-threshold", "npa-skipped",
         "closest-wins"],
)
def test_every_input_shape_gives_the_same_match(rows, kwargs):
    """Старые вызовы передают строки БД, прогон — пул: совпадение и его оценка одни."""
    from_rows = find_match(rows, **kwargs)
    from_prepared = find_match(prepare(rows), **kwargs)
    from_pool = find_match(pool(rows), **kwargs)

    assert from_rows == from_prepared == from_pool
    if from_rows is not None:
        assert from_pool.score == from_rows.score  # без «примерно»: это одно число
        assert from_pool.reason == from_rows.reason


def test_the_scores_of_a_cosine_match_are_identical_across_the_input_shapes():
    rows = [
        _candidate(1, simhash=simhash(NEWS_TEXT), embedding=encode_vector([0.9, 0.1, 0.0])),
        _candidate(2, simhash=simhash(NEWS_TEXT), embedding=encode_vector([1.0, 0.0, 0.0])),
    ]
    query = [0.95, 0.12, 0.0]

    matches = [
        find_match(shape, text_simhash=simhash(ARTICLE_TEXT), embedding=query, threshold=0.5)
        for shape in (rows, prepare(rows), pool(rows))
    ]

    assert len({(m.item_id, m.score, m.reason) for m in matches}) == 1
    assert matches[0].score == pytest.approx(
        max(cosine(query, [0.9, 0.1, 0.0]), cosine(query, [1.0, 0.0, 0.0]))
    )


def test_a_document_without_an_embedding_still_joins_by_simhash():
    pool = prepare([_candidate(7, simhash=simhash(ARTICLE_TEXT), embedding=None)])

    match = find_match(pool, text_simhash=simhash(REPRINT_TEXT), embedding=[])

    assert match.item_id == 7
    assert match.reason.startswith("simhash d=")


# ── match_text: заголовок карточки, к которой присоединяются ───────────────


def test_match_text_reads_the_title_from_raw_rows():
    rows = [_candidate(7, title="Комитет собрал отзывы"), _candidate(9, title="Другое")]

    assert match_text(rows, Match(7, 7, 1.0, "simhash d=0")) == "Комитет собрал отзывы"


def test_match_text_reads_the_title_from_a_prepared_pool():
    pool = prepare([_candidate(7, title="Комитет собрал отзывы")])

    assert match_text(pool, Match(7, 7, 1.0, "simhash d=0")) == "Комитет собрал отзывы"


def test_match_text_of_an_unknown_card_is_empty():
    assert match_text(prepare([_candidate(7, title="Заголовок")]), Match(99, 99, 1.0, "x")) == ""


# ── прогон готовит пул один раз ────────────────────────────────────────────


def _document(db, external_id: str, text: str) -> int:
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=f"Материал {external_id}",
                text=text,
                published_at=NOW,
                fetched_at=NOW,
            )
        )


def test_a_run_decodes_each_candidate_once_for_the_whole_batch(config, db, item_factory,
                                                               monkeypatch, frozen_clock):
    """Пул готовится один раз на прогон, а не заново на каждый документ."""
    carded = _document(db, "old", ARTICLE_TEXT)
    with db.transaction():
        db.documents.set_derived(
            carded, simhash=simhash(ARTICLE_TEXT), embedding=encode_vector([1.0, 0.0]),
            norm_text=ARTICLE_TEXT,
        )
    item_factory(db, carded, published_at=NOW)
    for index, text in enumerate((NEWS_TEXT, REPRINT_TEXT, "Совсем другой текст про спорт."), 1):
        _document(db, f"new{index}", text)
    service = ProcessingService(config, db, provider=None, embedder=None)
    rows = db.documents.unprocessed()
    calls: list[bytes | None] = []
    original = dedup.decode_vector
    monkeypatch.setattr(
        dedup, "decode_vector", lambda blob: calls.append(blob) or original(blob)
    )

    units = service._prepare(rows, embed=False)

    assert len(rows) == 3
    assert len(calls) == 1  # один кандидат — одно раскодирование на весь прогон
    assert any(u.join_item_id is not None for u in units)  # перепечатка нашла карточку
