"""src/api/routes/feed.py и items.py — читающие маршруты этапа 1.3 через `TestClient`.

Сервер не поднимается: всё идёт по ASGI напрямую (фикстура `client` из
tests/conftest.py открывает своё соединение к тому же файлу базы, что и корпус).
Обработчики обязаны только разбирать запрос и звать `FeedService`, поэтому здесь
проверяется разбор параметров, маршрутизация и формат ошибок, а не бизнес-логика.
"""

from __future__ import annotations

import pytest

PROBLEM = "application/problem+json"


def _ids(payload: dict) -> list[int]:
    return [row["id"] for row in payload["items"]]


def _names(corpus: dict[str, int], ids: list[int]) -> list[str]:
    by_id = {item_id: name for name, item_id in corpus.items()}
    return [by_id[i] for i in ids]


# ── лента ──────────────────────────────────────────────────────────────────


def test_the_feed_answers_with_rows_a_total_and_a_duration(client, corpus):
    response = client.get("/api/v1/items", params={"limit": 50})

    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == body["total"] == 6
    assert body["next_cursor"] is None
    assert isinstance(body["took_ms"], int)


def test_the_total_follows_the_filters_instead_of_counting_the_whole_base(client, corpus):
    whole = client.get("/api/v1/items", params={"limit": 1}).json()
    npa = client.get("/api/v1/items", params={"type": "npa", "limit": 1}).json()

    assert (whole["total"], len(whole["items"])) == (6, 1)
    assert (npa["total"], len(npa["items"])) == (2, 1)


def test_repeated_parameters_are_read_as_or_and_and(client, corpus):
    both_priorities = client.get(
        "/api/v1/items", params=[("priority", "high"), ("priority", "medium"), ("limit", 50)]
    ).json()
    both_tags = client.get(
        "/api/v1/items", params=[("tag", "регуляторика"), ("tag", "конкуренты"), ("limit", 50)]
    ).json()

    assert _names(corpus, _ids(both_priorities)) == [
        "hidden_digest",
        "npa_high",
        "news_medium",
        "npa_medium",
    ]
    assert _names(corpus, _ids(both_tags)) == ["news_medium"]


def test_source_id_is_repeatable(client, corpus, corpus_sources):
    body = client.get(
        "/api/v1/items",
        params=[
            ("source_id", corpus_sources["regulator"].id),
            ("source_id", corpus_sources["channel"].id),
            ("limit", 50),
        ],
    ).json()

    assert _names(corpus, _ids(body)) == ["news_medium", "npa_medium", "undated"]


def test_from_and_to_are_read_as_local_days(client, corpus):
    body = client.get("/api/v1/items", params={"from": "2026-09-02", "to": "2026-09-03"}).json()

    assert _names(corpus, _ids(body)) == ["news_medium", "news_low"]


def test_include_hidden_shows_every_state(client, corpus):
    visible = client.get("/api/v1/items", params={"limit": 50}).json()
    everything = client.get("/api/v1/items", params={"limit": 50, "include_hidden": True}).json()

    assert visible["total"] == 6
    assert everything["total"] == 8


def test_a_card_hidden_from_the_digest_is_still_in_the_http_feed(client, corpus):
    assert corpus["hidden_digest"] in _ids(client.get("/api/v1/items",
                                                   params={"limit": 50}).json())


def test_a_query_adds_a_highlighted_snippet_to_each_row(client, corpus):
    body = client.get("/api/v1/items", params={"q": "законопроект"}).json()

    assert _names(corpus, _ids(body)) == ["npa_high"]
    assert "<b>" in body["items"][0]["snippet"]


def test_the_row_carries_the_link_to_the_original_and_the_source_name(client, corpus):
    row = next(r for r in client.get("/api/v1/items", params={"limit": 50}).json()["items"]
               if r["id"] == corpus["npa_high"])

    assert row["canonical_url"] == "https://s1.example.ru/doc-1"
    assert row["source_name"] == "Ведомости"
    assert row["flags"] == {
        "degraded": False,
        "needs_review": False,
        "date_estimated": False,
        "edited": False,
    }


def test_the_cursor_from_one_page_fetches_the_next_one(client, corpus):
    first = client.get("/api/v1/items", params={"limit": 2}).json()

    second = client.get(
        "/api/v1/items", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()

    assert set(_ids(second)) & set(_ids(first)) == set()
    assert second["total"] == 6


# ── /items/facets не должен уходить в /items/{item_id} ─────────────────────


def test_the_facets_path_wins_over_the_card_path(client, corpus):
    response = client.get("/api/v1/items/facets")

    assert response.status_code == 200
    body = response.json()
    assert body["by_priority"] == {"high": 2, "medium": 2, "low": 2}
    assert body["by_type"] == {"npa": 2, "news": 4}
    assert body["total"] == 6


def test_the_facets_take_the_same_filters_as_the_feed(client, corpus):
    facets = client.get("/api/v1/items/facets", params={"from": "2026-09-04"}).json()
    feed = client.get("/api/v1/items", params={"from": "2026-09-04"}).json()

    assert facets["total"] == feed["total"] == 2
    assert [row["name"] for row in facets["by_source"]] == ["Ведомости"]
    assert [row["tag"] for row in facets["top_tags"]] == ["конкуренты", "регуляторика"]


# ── справочники ────────────────────────────────────────────────────────────


def test_filters_hands_the_panel_its_dictionaries(client, corpus, corpus_sources):
    body = client.get("/api/v1/filters").json()

    assert [s["name"] for s in body["sources"]] == ["Ведомости", "Банк России", "Канал ЦИТ"]
    assert body["tags"][0] == "регуляторика"
    assert body["priorities"] == ["high", "medium", "low"]
    assert body["types"] == ["npa", "news"]
    assert body["orders"] == ["published", "priority", "processed"]
    assert body["timezone"] == "Europe/Moscow"
    assert "внесён" in body["npa_statuses"]


# ── необработанное ─────────────────────────────────────────────────────────


def test_documents_lists_what_has_no_card_yet(client, corpus, corpus_sources, document_factory):
    document_factory(corpus_sources["media"], title="Ещё не обработан")

    body = client.get("/api/v1/documents", params={"unprocessed": True}).json()

    assert [row["title"] for row in body["documents"]] == ["Ещё не обработан"]
    assert body["total"] == 1


def test_documents_narrows_by_source_and_date(client, corpus_sources, document_factory):
    document_factory(corpus_sources["media"], title="Свежий",
                     published_at="2026-09-04T15:00:00+00:00")
    document_factory(corpus_sources["regulator"], title="Чужой",
                     published_at="2026-09-04T15:00:00+00:00")

    by_source = client.get(
        "/api/v1/documents", params={"source_id": corpus_sources["media"].id}
    ).json()
    by_date = client.get("/api/v1/documents", params={"from": "2026-09-05"}).json()

    assert [row["title"] for row in by_source["documents"]] == ["Свежий"]
    assert by_date["total"] == 0


# ── дайджест ───────────────────────────────────────────────────────────────


def test_the_digest_endpoint_renders_the_slice(client, corpus):
    response = client.post(
        "/api/v1/digest",
        json={"filters": {"priority": ["high"]}, "format": "markdown", "title": "Дайджест 05.09"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Дайджест 05.09"
    assert body["items"] == 1
    assert body["body"].startswith("# Дайджест 05.09")
    assert "## Высокий приоритет (1)" in body["body"]
    assert body["generated_at"]


def test_the_digest_endpoint_can_answer_in_json(client, corpus):
    body = client.post("/api/v1/digest", json={"filters": {}, "format": "json"}).json()

    assert body["format"] == "json"
    assert body["items"] == 5


def test_the_digest_endpoint_never_exports_a_card_hidden_from_the_digest(client, corpus):
    body = client.post("/api/v1/digest", json={"filters": {}}).json()

    assert "Скрыто из дайджеста" not in body["body"]


def test_the_digest_endpoint_saves_nothing(client, file_db, corpus):
    before = file_db.items.count()

    client.post("/api/v1/digest", json={"filters": {}, "include_notes": True})

    assert file_db.items.count() == before


# ── состояние ──────────────────────────────────────────────────────────────


def test_status_explains_the_gap_between_documents_and_cards(
    client, corpus, corpus_sources, document_factory
):
    document_factory(corpus_sources["media"], title="Ждёт обработки")

    body = client.get("/api/v1/status").json()

    assert (body["documents"], body["items"], body["unprocessed"]) == (9, 8, 1)
    assert body["sources"] == {"active": 3}
    assert body["timezone"] == "Europe/Moscow"


# ── карточка целиком ───────────────────────────────────────────────────────


def test_the_card_view_carries_the_link_to_the_original(client, corpus):
    body = client.get(f"/api/v1/items/{corpus['npa_high']}").json()

    assert body["canonical_url"] == "https://s1.example.ru/doc-1"
    assert body["item"]["title"] == "Минцифры внесло законопроект об аккредитации ИИ-сервисов"


def test_a_missing_card_is_still_a_404(client, corpus):
    response = client.get("/api/v1/items/9999")

    assert response.status_code == 404
    assert response.json()["code"] == "item_not_found"


# ── ошибки в формате problem+json ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/v1/items", {"limit": 500}),
        ("/api/v1/items", {"limit": 0}),
        ("/api/v1/items", {"type": "law"}),
        ("/api/v1/items", {"priority": "urgent"}),
        ("/api/v1/items", {"order": "relevance"}),
        ("/api/v1/items", {"type": "news", "npa_status": "внесён"}),
        ("/api/v1/items", {"from": "2026-09-05", "to": "2026-09-01"}),
        ("/api/v1/items", {"from": "вчера"}),
        ("/api/v1/items/facets", {"limit": 500}),
        ("/api/v1/documents", {"priority": "high"}),
        ("/api/v1/documents", {"type": "npa"}),
        ("/api/v1/documents", {"tag": "регуляторика"}),
        ("/api/v1/documents", {"npa_status": "внесён"}),
    ],
    ids=[
        "limit-too-big",
        "limit-zero",
        "unknown-type",
        "unknown-priority",
        "unknown-order",
        "npa-status-with-news",
        "from-after-to",
        "unparseable-date",
        "facets-share-the-checks",
        "documents-reject-priority",
        "documents-reject-type",
        "documents-reject-tag",
        "documents-reject-npa-status",
    ],
)
def test_a_bad_filter_is_a_validation_error_problem_document(client, corpus, path, params):
    response = client.get(path, params=params)

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["status"] == 400
    assert set(body) >= {"type", "title", "status", "detail", "code"}
    assert body["detail"]


@pytest.mark.parametrize(
    "cursor", ["!!!!", "не-курсор-вовсе", "eyJwIjogbnVsbH0="],
    ids=["not-base64", "cyrillic", "missing-keys"],
)
def test_a_malformed_cursor_is_an_invalid_cursor_problem_document(client, corpus, cursor):
    response = client.get("/api/v1/items", params={"cursor": cursor})

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "invalid_cursor"


def test_a_cursor_from_another_slice_is_refused_instead_of_paging_something_else(client, corpus):
    cursor = client.get("/api/v1/items", params={"limit": 2}).json()["next_cursor"]

    response = client.get("/api/v1/items", params={"limit": 2, "cursor": cursor, "priority": "high"})

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


@pytest.mark.parametrize(
    "params", [{"limit": "много"}, {"source_id": "не-число"}], ids=["limit", "source_id"]
)
def test_a_non_numeric_filter_is_a_validation_error_not_an_internal_one(client, corpus, params):
    """`int()` внутри `FeedQuery.build` обёрнут в `QueryValidationError` — 400, а не 500."""
    response = client.get("/api/v1/items", params=params)

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    assert "ожидалось целое число" in response.json()["detail"]


def test_an_empty_slice_is_a_200_with_an_empty_list_not_a_404(client, corpus):
    response = client.get("/api/v1/items", params={"tag": "такого-тега-нет"})

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "total": 0,
        "next_cursor": None,
        "took_ms": response.json()["took_ms"],
    }
