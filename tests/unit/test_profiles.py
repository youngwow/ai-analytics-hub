"""Профиль компании через API (план 1.4).

Профиль решает, что считается `high`, и целиком уходит в промпт — поэтому запись
сюда версионируется, а не перетирается: карточка навсегда помнит, какой версией
её оценили. Смена профиля по умолчанию — операция владельца продукта, и до этого
шага она жила только в CLI.
"""

from __future__ import annotations

import pytest
from support import ARTICLE_TEXT, NEWS_TEXT, FakeLLM, news_answer

from src.exceptions import ProfileError, ProfileNotFoundError, ProfileValidationError
from src.models import CompanyProfile, RawDocument, Source
from src.processing.profile import DEFAULT_NAME
from src.repositories import Database
from src.services.processing_service import ProcessingService
from src.services.profile_service import MAX_NAME, ProfileService

NOW = "2026-09-02T12:00:00+00:00"
PROBLEM = "application/problem+json"
PAYLOAD = {"industry": "ИТ", "topics": ["реестр отечественного ПО"]}
OTHER_PAYLOAD = {"industry": "ИТ и ИИ", "topics": ["КИИ"]}


@pytest.fixture
def service(config, db, frozen_clock) -> ProfileService:
    return ProfileService(config, db)


def _document(db: Database, external_id: str = "d1", text: str = NEWS_TEXT) -> int:
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title="Оператор платного ТВ запустил рекомендательный сервис",
                text=text,
                published_at=NOW,
                fetched_at=NOW,
            )
        )


# ── чтение ─────────────────────────────────────────────────────────────────


def test_a_fresh_hub_has_no_stored_profile_until_someone_asks(service):
    assert service.list_profiles() == []


def test_saved_profiles_are_listed_in_the_order_they_appeared(service):
    first = service.save("ООО «Цифра»", PAYLOAD)
    second = service.save("ООО «Второй»", PAYLOAD)

    assert [p.id for p in service.list_profiles()] == [first.id, second.id]


def test_get_profile_returns_the_stored_payload(service):
    saved = service.save("ООО «Цифра»", PAYLOAD)

    stored = service.get_profile(saved.id)

    assert (stored.name, stored.payload, stored.version) == ("ООО «Цифра»", PAYLOAD, 1)
    assert stored.updated_at == NOW


def test_an_unknown_profile_is_a_named_404(service):
    with pytest.raises(ProfileNotFoundError, match="#999") as excinfo:
        service.get_profile(999)

    assert (excinfo.value.code, excinfo.value.status_code) == ("profile_not_found", 404)


def test_the_active_profile_is_created_on_the_first_ask(service):
    """Иначе «чем считается приоритет» — вопрос без ответа на новой установке."""
    active = service.active()

    assert active.name == DEFAULT_NAME
    assert active.is_default is True
    assert [p.id for p in service.list_profiles()] == [active.id]


def test_asking_twice_does_not_create_a_second_default(service):
    first = service.active()

    assert service.active().id == first.id
    assert len(service.list_profiles()) == 1


# ── запись: имя — ключ, версия — история ───────────────────────────────────


def test_saving_a_new_name_creates_version_one(service, db):
    saved = service.save("ООО «Цифра»", PAYLOAD)

    assert (saved.version, saved.payload) == (1, PAYLOAD)
    assert db.profiles.get(saved.id).payload == PAYLOAD


def test_saving_the_same_name_twice_bumps_the_version_and_keeps_one_row(service):
    first = service.save("ООО «Цифра»", PAYLOAD)

    second = service.save("ООО «Цифра»", OTHER_PAYLOAD)

    assert (second.id, second.version) == (first.id, 2)
    assert len(service.list_profiles()) == 1
    assert service.get_profile(first.id).payload == OTHER_PAYLOAD


def test_the_name_is_stripped_before_it_becomes_the_key(service):
    service.save("  ООО «Цифра»  ", PAYLOAD)

    saved = service.save("ООО «Цифра»", OTHER_PAYLOAD)

    assert saved.version == 2
    assert [p.name for p in service.list_profiles()] == ["ООО «Цифра»"]


@pytest.mark.parametrize("name", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_a_profile_without_a_name_is_refused(service, name):
    with pytest.raises(ProfileValidationError, match="имя"):
        service.save(name, PAYLOAD)


def test_an_oversized_name_is_refused(service):
    with pytest.raises(ProfileValidationError, match=f"длиннее {MAX_NAME}"):
        service.save("я" * (MAX_NAME + 1), PAYLOAD)


def test_a_name_of_exactly_the_limit_is_accepted(service):
    assert service.save("я" * MAX_NAME, PAYLOAD).version == 1


@pytest.mark.parametrize(
    "payload", [{}, [], "профиль", None, 0], ids=["empty", "list", "string", "none", "zero"]
)
def test_a_payload_that_is_not_a_non_empty_object_is_refused(service, payload):
    with pytest.raises(ProfileValidationError, match="непустой объект"):
        service.save("ООО «Цифра»", payload)


def test_nothing_is_stored_when_the_request_is_refused(service):
    with pytest.raises(ProfileValidationError):
        service.save("", PAYLOAD)

    assert service.list_profiles() == []


# ── профиль по умолчанию ───────────────────────────────────────────────────


def test_set_default_moves_the_flag_and_changes_what_active_returns(service):
    first = service.save("ООО «Первый»", PAYLOAD)
    second = service.save("ООО «Второй»", OTHER_PAYLOAD)
    service.set_default(first.id)

    switched = service.set_default(second.id)

    assert switched.is_default is True
    assert service.active().id == second.id
    assert [p.is_default for p in service.list_profiles()] == [False, True]


def test_set_default_of_an_unknown_profile_is_a_named_404(service):
    with pytest.raises(ProfileNotFoundError, match="#999") as excinfo:
        service.set_default(999)

    assert excinfo.value.code == "profile_not_found"


def test_the_service_errors_stay_inside_one_family(service):
    """CLI и обработчик ловят `ProfileError` семейством."""
    with pytest.raises(ProfileError):
        service.save("", PAYLOAD)
    with pytest.raises(ProfileError):
        service.get_profile(999)


def test_a_card_processed_after_the_switch_records_the_new_profile_version(config, db,
                                                                          frozen_clock):
    profiles = ProfileService(config, db)
    chosen = profiles.save("ООО «Второй»", OTHER_PAYLOAD)
    profiles.set_default(chosen.id)
    document_id = _document(db)

    ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None).run()

    item_id = db.items.item_for_document(document_id)
    assert db.items.get(item_id).profile_version == chosen.version


def test_saving_a_new_version_of_the_active_profile_moves_the_next_card(config, db, frozen_clock):
    profiles = ProfileService(config, db)
    profiles.set_default(profiles.save("ООО «Цифра»", PAYLOAD).id)
    processing = ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None)
    first_document = _document(db, "d1")
    processing.run()

    profiles.save("ООО «Цифра»", OTHER_PAYLOAD)
    # Другой текст: перепечатка присоединилась бы к первой карточке и версию не показала.
    second_document = _document(db, "d2", text=ARTICLE_TEXT)
    processing.run()

    versions = [
        db.items.get(db.items.item_for_document(document)).profile_version
        for document in (first_document, second_document)
    ]
    assert versions == [1, 2]


# ── HTTP ───────────────────────────────────────────────────────────────────


def test_the_profile_list_of_an_empty_hub_is_empty(client):
    response = client.get("/api/v1/profiles")

    assert response.status_code == 200
    assert response.json() == {"profiles": []}


def test_saving_a_profile_answers_201_with_the_stored_row(client, file_db):
    response = client.post(
        "/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": PAYLOAD}
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["payload"], body["version"]) == ("ООО «Цифра»", PAYLOAD, 1)
    assert body["is_default"] is False
    stored = file_db.profiles.get(body["id"])
    assert stored.payload == PAYLOAD
    assert body["updated_at"] == stored.updated_at
    assert body["updated_at"].endswith("+00:00")


def test_the_created_profile_is_stamped_in_the_answer_not_only_in_the_table(client, file_db):
    """Иначе тело ответа на создание расходится с тем, что потом отдаёт чтение."""
    created = client.post(
        "/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": PAYLOAD}
    ).json()

    bumped = client.post(
        "/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": OTHER_PAYLOAD}
    ).json()

    assert created["updated_at"].endswith("+00:00")
    assert bumped["updated_at"] == file_db.profiles.get(bumped["id"]).updated_at
    assert bumped["updated_at"] != ""


def test_the_service_returns_the_stamp_of_both_the_insert_and_the_bump(service, db):
    created = service.save("ООО «Цифра»", PAYLOAD)

    bumped = service.save("ООО «Цифра»", OTHER_PAYLOAD)

    assert (created.updated_at, bumped.updated_at) == (NOW, NOW)
    assert db.profiles.get(bumped.id).updated_at == NOW


def test_saving_the_same_name_twice_over_http_bumps_the_version(client):
    first = client.post("/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": PAYLOAD})

    second = client.post(
        "/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": OTHER_PAYLOAD}
    )

    assert second.json()["id"] == first.json()["id"]
    assert (first.json()["version"], second.json()["version"]) == (1, 2)
    assert len(client.get("/api/v1/profiles").json()["profiles"]) == 1


def test_the_active_profile_is_served_from_a_literal_path(client):
    """`/profiles/active` объявлен раньше `/{profile_id}` — иначе это 422 на разборе int."""
    response = client.get("/api/v1/profiles/active")

    assert response.status_code == 200
    assert response.json()["name"] == DEFAULT_NAME
    assert response.json()["is_default"] is True


def test_one_profile_is_served_by_id(client):
    created = client.post(
        "/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": PAYLOAD}
    ).json()

    response = client.get(f"/api/v1/profiles/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


def test_an_unknown_profile_is_a_404_problem(client):
    response = client.get("/api/v1/profiles/999")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "profile_not_found"


def test_making_a_profile_default_changes_what_active_returns(client):
    created = client.post(
        "/api/v1/profiles", json={"name": "ООО «Второй»", "payload": OTHER_PAYLOAD}
    ).json()

    response = client.post(f"/api/v1/profiles/{created['id']}/default")

    assert response.status_code == 200
    assert response.json()["is_default"] is True
    assert client.get("/api/v1/profiles/active").json()["id"] == created["id"]


def test_making_an_unknown_profile_default_is_a_404_problem(client):
    response = client.post("/api/v1/profiles/999/default")

    assert response.status_code == 404
    assert response.json()["code"] == "profile_not_found"


@pytest.mark.parametrize(
    "payload",
    [
        {"payload": PAYLOAD},
        {"name": "ООО «Цифра»"},
        {"name": "", "payload": PAYLOAD},
        {"name": "я" * 201, "payload": PAYLOAD},
        {"name": "ООО «Цифра»", "payload": "не объект"},
        {"name": "ООО «Цифра»", "payload": PAYLOAD, "is_default": True},
    ],
    ids=["no-name", "no-payload", "empty-name", "long-name", "payload-not-an-object",
         "unknown-field"],
)
def test_a_malformed_profile_request_is_rejected_by_the_schema(client, file_db, payload):
    response = client.post("/api/v1/profiles", json=payload)

    assert response.status_code == 422
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    assert file_db.profiles.list() == []


def test_a_blank_name_passes_the_schema_and_is_stopped_by_the_service(client, file_db):
    response = client.post("/api/v1/profiles", json={"name": "   ", "payload": PAYLOAD})

    assert response.status_code == 400
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["code"] == "validation_error"
    assert file_db.profiles.list() == []


def test_an_empty_payload_is_a_400_problem(client, file_db):
    response = client.post("/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": {}})

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert file_db.profiles.list() == []


def test_the_list_shows_the_default_flag_after_a_switch(client):
    created = client.post(
        "/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": PAYLOAD}
    ).json()
    client.post(f"/api/v1/profiles/{created['id']}/default")

    profiles = client.get("/api/v1/profiles").json()["profiles"]

    assert [(p["id"], p["is_default"]) for p in profiles] == [(created["id"], True)]


def test_the_stored_profile_is_what_the_repository_returns(client, file_db):
    created = client.post(
        "/api/v1/profiles", json={"name": "ООО «Цифра»", "payload": PAYLOAD}
    ).json()

    stored = file_db.profiles.get(created["id"])

    assert stored.updated_at.endswith("+00:00")
    assert stored == CompanyProfile(
        id=created["id"],
        name="ООО «Цифра»",
        payload=PAYLOAD,
        version=1,
        is_default=False,
        updated_at=stored.updated_at,
    )
