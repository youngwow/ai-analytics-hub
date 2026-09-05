"""src/services/item_service.py::add_npa_event — хронология НПА и `POST /items/{id}/events`.

Статус из словаря двигает карточку НПА только вперёд и никогда не затирает правку
человека; новость от события статуса не получает; слушания и сроки — просто
строки истории. Дата события хранится в UTC: голая дата — это московские сутки.
"""

from __future__ import annotations

import pytest

from src.exceptions import ItemError
from src.models import RawDocument, Source
from src.services.item_service import ItemService

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"


def _card(db, item_factory, external_id: str, **fields) -> int:
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        document_id = db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=f"Материал {external_id}",
                fetched_at=NOW,
            )
        )
    return item_factory(db, document_id, **fields)


@pytest.fixture
def service(config, db, frozen_clock) -> ItemService:
    return ItemService(config, db)


@pytest.fixture
def cards(db, item_factory) -> dict[str, int]:
    """Четыре карточки: НПА «внесён», новость, НПА с правкой статуса, НПА без статуса."""
    return {
        "npa": _card(db, item_factory, "a", type="npa", npa_status="внесён", npa_key="1-8"),
        "news": _card(db, item_factory, "b", type="news"),
        "overridden": _card(
            db, item_factory, "c", type="npa", npa_status="внесён",
            manual_overrides=["npa_status"],
        ),
        "fresh": _card(db, item_factory, "d", type="npa", npa_status=None),
    }


# ── запись события ─────────────────────────────────────────────────────────


def test_the_event_is_stored_with_its_id_and_the_frozen_created_at(service, db, cards):
    event = service.add_npa_event(
        cards["npa"],
        "рассмотрение",
        occurred_at="2026-09-03T10:00:00+00:00",
        source_url="https://sozd.duma.gov.ru/bill/1-8",
        note="первое чтение",
    )

    assert event.id is not None
    assert (event.item_id, event.status, event.created_by, event.created_at) == (
        cards["npa"], "рассмотрение", "user", NOW
    )
    stored = db.items.events(cards["npa"])
    assert [(e.id, e.status, e.occurred_at, e.source_url, e.note, e.created_at) for e in stored] == [
        (
            event.id, "рассмотрение", "2026-09-03T10:00:00+00:00",
            "https://sozd.duma.gov.ru/bill/1-8", "первое чтение", NOW,
        )
    ]


def test_the_actor_is_recorded_as_created_by(service, db, cards):
    event = service.add_npa_event(cards["npa"], "принят", actor="system")

    assert db.items.events(cards["npa"])[0].created_by == event.created_by == "system"


def test_the_status_is_stripped_before_it_is_stored(service, db, cards):
    event = service.add_npa_event(cards["npa"], "  принят  ")

    assert event.status == "принят"
    assert db.items.get(cards["npa"]).npa_status == "принят"


def test_a_status_of_exactly_64_characters_is_accepted(service, db, cards):
    status = "с" * 64

    event = service.add_npa_event(cards["npa"], status)

    assert event.status == status


def test_the_card_view_lists_the_events_in_chronological_order(service, cards):
    service.add_npa_event(cards["npa"], "принят", occurred_at="2026-09-10")
    service.add_npa_event(cards["npa"], "слушания", occurred_at="2026-09-05")
    service.add_npa_event(cards["npa"], "срок", occurred_at=None)

    events = service.get_item(cards["npa"])["events"]

    # Событие без даты идёт первым: NULL меньше любой строки при сортировке по возрастанию.
    assert [e.status for e in events] == ["срок", "слушания", "принят"]


# ── статус карточки ────────────────────────────────────────────────────────


def test_a_later_status_advances_an_npa_card(service, db, cards):
    service.add_npa_event(cards["npa"], "рассмотрение")

    assert db.items.get(cards["npa"]).npa_status == "рассмотрение"


@pytest.mark.parametrize("status", ["анонс", "разработка", "внесён"], ids=lambda s: s)
def test_an_earlier_or_equal_status_never_moves_the_card_back(service, db, cards, status):
    service.add_npa_event(cards["npa"], status)

    assert db.items.get(cards["npa"]).npa_status == "внесён"
    assert [e.status for e in db.items.events(cards["npa"])] == [status]


def test_a_card_without_a_status_takes_the_first_one_it_is_given(service, db, cards):
    service.add_npa_event(cards["fresh"], "анонс")

    assert db.items.get(cards["fresh"]).npa_status == "анонс"


def test_a_news_card_never_gets_an_npa_status_from_an_event(service, db, cards):
    service.add_npa_event(cards["news"], "принят")

    item = db.items.get(cards["news"])
    assert (item.type, item.npa_status) == ("news", None)
    assert [e.status for e in db.items.events(cards["news"])] == ["принят"]


def test_a_human_override_of_the_status_is_never_moved_by_an_event(service, db, cards):
    service.add_npa_event(cards["overridden"], "принят")

    item = db.items.get(cards["overridden"])
    assert item.npa_status == "внесён"
    assert item.manual_overrides == ["npa_status"]
    assert [e.status for e in db.items.events(cards["overridden"])] == ["принят"]


@pytest.mark.parametrize(
    "status", ["слушания", "срок", "Второе чтение", "hearing"], ids=lambda s: s
)
def test_a_free_form_status_is_stored_but_never_advances_the_card(service, db, cards, status):
    service.add_npa_event(cards["npa"], status)

    assert db.items.get(cards["npa"]).npa_status == "внесён"
    assert [e.status for e in db.items.events(cards["npa"])] == [status]


def test_advancing_by_events_does_not_mark_the_status_as_a_human_edit(service, db, cards):
    service.add_npa_event(cards["npa"], "принят")

    assert db.items.get(cards["npa"]).manual_overrides == []


# ── дата события ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("2026-09-03", "2026-09-02T21:00:00+00:00"),
        ("2026-09-03T10:00:00", "2026-09-03T07:00:00+00:00"),
        ("2026-09-03T10:00:00+05:00", "2026-09-03T05:00:00+00:00"),
        ("2026-09-03T10:00:00Z", "2026-09-03T10:00:00+00:00"),
        ("Thu, 03 Sep 2026 10:00:00 +0300", "2026-09-03T07:00:00+00:00"),
        (None, None),
        ("", None),
    ],
    ids=[
        "bare-date-is-moscow-midnight", "naive-is-moscow", "offset", "zulu", "rfc2822", "none",
        "empty-is-none",
    ],
)
def test_occurred_at_is_normalised_to_utc(service, db, cards, given, stored):
    event = service.add_npa_event(cards["npa"], "принят", occurred_at=given)

    assert event.occurred_at == stored
    assert db.items.events(cards["npa"])[0].occurred_at == stored


@pytest.mark.parametrize(
    "given", ["вчера", "03.09.2026", "2026-13-40", "   "], ids=["word", "dotted", "impossible", "spaces"]
)
def test_an_unparseable_date_is_a_validation_error_and_nothing_is_stored(
    service, db, cards, given
):
    with pytest.raises(ItemError, match="дата события не разбирается") as excinfo:
        service.add_npa_event(cards["npa"], "принят", occurred_at=given)

    assert excinfo.value.code == "validation_error"
    assert db.items.events(cards["npa"]) == []
    assert db.items.get(cards["npa"]).npa_status == "внесён"


# ── проверки ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_an_empty_status_is_a_validation_error(service, db, cards, status):
    with pytest.raises(ItemError, match="пустой статус") as excinfo:
        service.add_npa_event(cards["npa"], status)

    assert excinfo.value.code == "validation_error"
    assert db.items.events(cards["npa"]) == []


def test_a_status_longer_than_64_characters_is_refused(service, db, cards):
    with pytest.raises(ItemError, match="64") as excinfo:
        service.add_npa_event(cards["npa"], "с" * 65)

    assert excinfo.value.code == "validation_error"
    assert db.items.events(cards["npa"]) == []


def test_an_unknown_card_is_a_named_404(service):
    with pytest.raises(ItemError, match="#999") as excinfo:
        service.add_npa_event(999, "принят")

    assert (excinfo.value.code, excinfo.value.status_code) == ("item_not_found", 404)


# ── HTTP ───────────────────────────────────────────────────────────────────


@pytest.fixture
def npa_card(file_db, item_factory, frozen_clock) -> int:
    return _card(file_db, item_factory, "http", type="npa", npa_status="внесён")


def test_posting_an_event_answers_201_with_the_stored_event(client, file_db, npa_card):
    response = client.post(
        f"/api/v1/items/{npa_card}/events",
        json={
            "status": "рассмотрение",
            "occurred_at": "2026-09-03",
            "source_url": "https://sozd.duma.gov.ru/bill/1-8",
            "note": "первое чтение",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["id"] is not None
    assert body == {
        "id": body["id"],
        "item_id": npa_card,
        "status": "рассмотрение",
        "occurred_at": "2026-09-02T21:00:00+00:00",
        "source_url": "https://sozd.duma.gov.ru/bill/1-8",
        "note": "первое чтение",
        "created_by": "user",
        "created_at": NOW,
    }
    assert file_db.items.get(npa_card).npa_status == "рассмотрение"


def test_an_empty_occurred_at_over_http_is_stored_as_null(client, file_db, npa_card):
    response = client.post(
        f"/api/v1/items/{npa_card}/events", json={"status": "слушания", "occurred_at": ""}
    )

    assert response.status_code == 201
    assert response.json()["occurred_at"] is None
    assert file_db.items.events(npa_card)[0].occurred_at is None


def test_the_card_view_lists_the_posted_event(client, npa_card):
    posted = client.post(f"/api/v1/items/{npa_card}/events", json={"status": "слушания"}).json()

    card = client.get(f"/api/v1/items/{npa_card}").json()

    assert [(e["id"], e["status"]) for e in card["events"]] == [(posted["id"], "слушания")]
    assert card["item"]["npa_status"] == "внесён"


def test_an_event_for_an_unknown_card_is_a_404_problem(client):
    response = client.post("/api/v1/items/999/events", json={"status": "принят"})

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "item_not_found"


def test_a_bad_date_is_a_400_validation_error_problem(client, file_db, npa_card):
    response = client.post(
        f"/api/v1/items/{npa_card}/events", json={"status": "принят", "occurred_at": "вчера"}
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    assert file_db.items.events(npa_card) == []


@pytest.mark.parametrize(
    "payload",
    [{"status": ""}, {"status": "с" * 65}, {}, {"status": "принят", "actor": "user"}],
    ids=["empty-status", "status-too-long", "no-status", "unknown-field"],
)
def test_a_bad_body_is_rejected_by_the_request_schema(client, file_db, npa_card, payload):
    response = client.post(f"/api/v1/items/{npa_card}/events", json=payload)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    assert file_db.items.events(npa_card) == []
