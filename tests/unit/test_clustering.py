"""src/processing/clustering.py и шаг прогона после модели: «вероятный дубль — объединить?».

Чистая часть проверяется на синтетических векторах: пара похожих карточек
попадает в одну группу, заведомо разные — нет, одиночка остаётся шумом.
Сервисная часть — на прогоне с `FakeLLM`: саммари задаёт ответ модели, вектор
саммари — поддельный эмбеддер, а S1 при этом ничего не схлопывает (векторы
документов ортогональны), так что до кластеризации доходят две карточки.
"""

from __future__ import annotations

import json
import logging

import pytest
import torch
from support import FakeLLM, news_answer

from src.config import ClusteringConfig, Config
from src.models import RawDocument, Source
from src.processing import clustering
from src.processing.clustering import DuplicateGroup, find_groups, mean_similarity, reduce
from src.services.item_service import DUPLICATE_FIELD, ItemService
from src.services.processing_service import ProcessingService

NOW = "2026-09-02T12:00:00+00:00"
SETTINGS = ClusteringConfig(min_cluster_size=2, min_samples=1, reduction="none")
LOOSE = ClusteringConfig(min_cluster_size=2, min_samples=1, reduction="none", min_similarity=0)


def _unit(*values: float) -> torch.Tensor:
    vector = torch.tensor(values, dtype=torch.float32)
    return vector / vector.norm()


def _cloud() -> tuple[list[int], torch.Tensor]:
    """Девять карточек в 6 измерениях: пара 1–2 близка, тройка 3–5 близка, остальные врозь."""
    rows = [
        _unit(1.0, 0.10, 0, 0, 0, 0),  # 1  ┐ пограничная пара: косинус ≈ 0.98
        _unit(1.0, -0.10, 0, 0, 0, 0),  # 2 ┘
        _unit(0, 0, 1.0, 0.05, 0, 0),  # 3  ┐
        _unit(0, 0, 1.0, -0.05, 0, 0),  # 4  │ тройка
        _unit(0, 0, 1.0, 0.0, 0.05, 0),  # 5 ┘
        _unit(0, 0, 0, 0, 1.0, 0),  # 6 одиночка
        _unit(0, 0, 0, 0, 0, 1.0),  # 7 одиночка
        _unit(0.5, 0, 0.5, 0, 0.5, 0.5),  # 8 одиночка посередине
        _unit(0, 1.0, 0, 0, 0, 0),  # 9 одиночка
    ]
    return list(range(1, 10)), torch.stack(rows)


def _by_member(groups: list[DuplicateGroup]) -> dict[int, tuple[int, ...]]:
    return {member: group.item_ids for group in groups for member in group.item_ids}


# ── чистая кластеризация ───────────────────────────────────────────────────


def test_a_borderline_pair_is_flagged_and_a_singleton_is_left_alone():
    ids, matrix = _cloud()

    groups = find_groups(ids, matrix, SETTINGS)
    members = _by_member(groups)

    assert members[1] == (1, 2)  # пограничная пара — в одной группе
    assert members[3] == (3, 4, 5)
    for singleton in (6, 7, 8, 9):
        assert singleton not in members  # шум (label = −1) не трогается


def test_a_confidently_different_pair_is_not_flagged():
    ids, matrix = _cloud()
    groups = find_groups(ids, matrix, SETTINGS)
    members = _by_member(groups)
    assert members.get(1) != members.get(6)
    assert 9 not in members  # похож на 1 только по одному измерению: косинус ≈ 0.1


def test_loosely_attached_cards_are_pruned_from_a_group():
    """Без порога снизу одиночки, «упавшие» из группы по дороге к её ядру, носят её метку."""
    ids, matrix = _cloud()
    loose = _by_member(find_groups(ids, matrix, LOOSE))
    strict = _by_member(find_groups(ids, matrix, SETTINGS))
    assert set(loose.get(3, ())) > {3, 4, 5}  # HDBSCAN сам по себе тянет соседей в тройку
    assert strict[3] == (3, 4, 5)


def test_a_chain_of_pairwise_neighbours_does_not_become_one_group():
    """A~B и B~C выше порога, A~C — нет: связные компоненты склеили бы всех троих."""
    ids = [1, 2, 3, 4]
    matrix = torch.stack(
        [
            _unit(1.0, 0.0, 0, 0),
            _unit(0.75, 0.66, 0, 0),  # ≈0.75 к 1 и ≈0.75 к 3
            _unit(0.0, 1.0, 0, 0),  # ≈0 к 1
            _unit(0, 0, 0, 1.0),
        ]
    )
    members = _by_member(find_groups(ids, matrix, SETTINGS))
    assert all(len(group) == 2 for group in members.values())
    assert not (1 in members and 3 in members and members[1] == members[3])


def test_two_groups_glued_by_hdbscan_are_split_into_components():
    """Пара и тройка в одном кластере HDBSCAN (epsilon склеил всё): компоненты разводят их."""
    ids, matrix = _cloud()
    glued = ClusteringConfig(
        min_cluster_size=2, min_samples=1, reduction="none", cluster_selection_epsilon=2.0
    )
    assert len({g.label for g in find_groups(ids, matrix, LOOSE) if True}) >= 1
    members = _by_member(find_groups(ids, matrix, glued))
    assert members.get(1) == (1, 2) and members.get(3) == (3, 4, 5)
    assert 8 not in members


def test_a_pair_among_singletons_is_still_found():
    """Пул из одной пары и одиночек: дерево кластеров — один корень (allow_single_cluster)."""
    ids = [1, 2, 3, 4]
    matrix = torch.stack(
        [_unit(1.0, 0.1, 0, 0), _unit(1.0, -0.1, 0, 0), _unit(0, 0, 1.0, 0), _unit(0, 0, 0.3, 1.0)]
    )
    members = _by_member(find_groups(ids, matrix, SETTINGS))
    assert members == {1: (1, 2), 2: (1, 2)}


def test_a_mutual_nearest_pair_far_apart_is_not_a_duplicate_group():
    """HDBSCAN считает кластером любую пару взаимных соседей; порог снизу это отсекает."""
    ids = [1, 2, 3, 4]
    matrix = torch.stack(
        [_unit(1.0, 0.1, 0, 0), _unit(1.0, -0.1, 0, 0), _unit(0, 0, 1.0, 0), _unit(0, 0, 0.3, 1.0)]
    )
    loose = _by_member(find_groups(ids, matrix, LOOSE))
    assert loose.get(3) == (3, 4)  # косинус 0.29 — а для HDBSCAN это группа
    assert 3 not in _by_member(find_groups(ids, matrix, SETTINGS))


def test_group_similarity_is_the_mean_pairwise_cosine_of_the_original_vectors():
    ids, matrix = _cloud()
    groups = {group.item_ids: group for group in find_groups(ids, matrix, SETTINGS)}
    pair = groups[(1, 2)]
    expected = float(matrix[0] @ matrix[1])
    assert pair.similarity == pytest.approx(expected, abs=1e-3)
    assert pair.partners(1) == [2]
    assert mean_similarity(torch.stack([matrix[0], matrix[0]])) == pytest.approx(1.0)


def _pool_with_big_and_small_groups() -> tuple[list[int], torch.Tensor]:
    """Две плотные группы по пять карточек, пара и тройка — в 8 измерениях."""
    torch.manual_seed(1)
    rows, ids = [], []

    def around(axis: int, count: int, first_id: int) -> None:
        for offset in range(count):
            base = torch.zeros(8)
            base[axis] = 1.0
            rows.append(torch.nn.functional.normalize(base + 0.05 * torch.randn(8), dim=0))
            ids.append(first_id + offset)

    around(0, 5, 10)
    around(1, 5, 20)
    around(2, 2, 30)
    around(3, 3, 40)
    return ids, torch.stack(rows)


def test_min_cluster_size_five_leaves_pairs_and_triples_as_noise():
    """Значение из постановки (5/5) на дублях не срабатывает: группы дублей — это 2–3 карточки."""
    ids, matrix = _pool_with_big_and_small_groups()
    strict = ClusteringConfig(min_cluster_size=5, min_samples=5, reduction="none")
    found = {group.item_ids for group in find_groups(ids, matrix, strict)}
    assert (30, 31) not in found and (40, 41, 42) not in found  # пара и тройка — шум

    calibrated = ClusteringConfig(min_cluster_size=2, min_samples=1, reduction="none")
    found = {group.item_ids for group in find_groups(ids, matrix, calibrated)}
    assert found == {(10, 11, 12, 13, 14), (20, 21, 22, 23, 24), (30, 31), (40, 41, 42)}


def test_too_few_points_are_all_noise():
    ids, matrix = _cloud()
    assert find_groups(ids[:2], matrix[:2], SETTINGS) == []
    assert find_groups([], matrix[:0], SETTINGS) == []


def test_a_shape_mismatch_yields_nothing():
    ids, matrix = _cloud()
    assert find_groups(ids[:3], matrix, SETTINGS) == []


def test_pca_keeps_rows_normalised_and_the_groups_intact():
    ids, matrix = _cloud()

    reduced = reduce(matrix, "pca", 3)
    assert reduced.shape == (9, 3)
    assert torch.allclose(reduced.norm(dim=1), torch.ones(9), atol=1e-5)

    settings = ClusteringConfig(min_cluster_size=2, min_samples=1, reduction="pca", n_components=4)
    members = _by_member(find_groups(ids, matrix, settings))
    assert members[1] == (1, 2) and set(members[3]) >= {3, 4, 5}
    assert 9 not in members


def test_reduction_none_or_zero_components_returns_the_input():
    ids, matrix = _cloud()
    assert reduce(matrix, "none", 3) is matrix
    assert reduce(matrix, "pca", 0) is matrix
    assert reduce(matrix[:2], "pca", 3) is matrix[:2] or reduce(matrix[:2], "pca", 3).shape == (
        2,
        6,
    )


def test_umap_on_a_handful_of_points_falls_back_to_pca(caplog):
    ids, matrix = _cloud()
    with caplog.at_level(logging.INFO, logger="clustering"):
        reduced = reduce(matrix, "umap", 3)
    assert reduced.shape == (9, 3)
    assert "мало для UMAP" in caplog.text


def test_umap_without_the_package_is_a_clear_error(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_umap(name, *args, **kwargs):
        if name == "umap":
            raise ImportError("no module named umap")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_umap)
    matrix = torch.nn.functional.normalize(torch.randn(20, 6), dim=1)
    with pytest.raises(RuntimeError, match="uv sync --extra umap"):
        clustering._umap(matrix, 3, 0)


@pytest.mark.slow
def test_umap_reduction_groups_the_same_duplicates():
    pytest.importorskip("umap")
    torch.manual_seed(0)
    ids, matrix = _cloud()
    # UMAP хочет хотя бы десяток точек: добавим одиночек-шумов.
    extra = torch.nn.functional.normalize(torch.randn(12, 6), dim=1)
    ids = ids + list(range(100, 112))
    settings = ClusteringConfig(min_cluster_size=2, min_samples=1, reduction="umap", n_components=2)

    members = _by_member(find_groups(ids, torch.cat([matrix, extra]), settings))

    assert set(members.get(1, ())) >= {1, 2}
    assert set(members.get(3, ())) >= {3, 4, 5}


# ── прогон: предложение записывается как ревизия модели ────────────────────

GROUP_A = ["Группа А: регулятор ограничил доступ к VPN.", "Причина — нарушение закона.", "Итого."]
GROUP_A2 = ["Группа А: РКН заблокировал VPN-сервисы.", "Основание — закон.", "Итого."]
GROUP_B = ["Группа Б: оператор запустил рекомендательный сервис.", "Бета доступна.", "Итого."]
GROUP_C = ["Группа В: биржа объявила о новом индексе.", "Торги стартуют в понедельник.", "Итого."]
NPA_KEY = "112233-8"


_SLOTS: dict[str, int] = {}


def _vector(text: str, position: int) -> list[float]:
    """Саммари одной группы — близкие векторы; документы — ортогональные (S1 молчит).

    Слот документа закреплён за текстом, а не за позицией в батче: иначе новый
    документ следующего прогона получил бы вектор старого и S1 бы их склеил.
    """
    width = 16
    row = [0.0] * width
    if "Группа А" in text:
        row[0] = 1.0
        row[1] = 0.1 if "РКН" in text else -0.1
    elif "Группа Б" in text:
        row[2] = 1.0
    elif "Группа В" in text:
        row[3] = 1.0
    else:
        slot = _SLOTS.setdefault(text, len(_SLOTS) % 12)
        row[4 + slot] = 1.0
    return row


def _embedder(fail_on_summaries: bool = False):
    def embed(texts):
        if fail_on_summaries and any("Группа" in text for text in texts):
            raise RuntimeError("эмбеддер саммари сломан")
        return [_vector(text, index) for index, text in enumerate(texts)]

    return embed


def _answer(prompt: str) -> dict:
    """Ответ модели зависит от документа: разные события — разные саммари."""
    if "Второй пересказ" in prompt:
        return news_answer(summary=GROUP_A2)
    if "Первый пересказ" in prompt:
        return news_answer(summary=GROUP_A)
    if "Ещё одна" in prompt:
        return news_answer(summary=GROUP_C)
    return news_answer(summary=GROUP_B)


def _npa_answer(prompt: str) -> dict:
    return {
        **news_answer(summary=GROUP_A),
        "type": "npa",
        "npa_key": NPA_KEY,
        "npa_status": "внесён",
    }


def _documents(db, *texts: str) -> list[int]:
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    ids = []
    with db.transaction():
        for index, text in enumerate(texts):
            ids.append(
                db.documents.insert(
                    RawDocument(
                        source_id=source.id,
                        external_id=f"d{db.documents.count() + 1}",
                        url=f"https://a.ru/{db.documents.count() + 1}",
                        title=text.split(".")[0],
                        text=text,
                        published_at=NOW,
                        fetched_at=NOW,
                    )
                )
            )
    return ids


@pytest.fixture
def cluster_config(raw_config) -> Config:
    raw_config["embeddings"] = {"provider": "local", "dimensions": 16}
    raw_config["clustering"] = {"min_cluster_size": 2, "min_samples": 1, "reduction": "none"}
    return Config.from_dict(raw_config)


@pytest.fixture
def service(cluster_config, db, frozen_clock) -> ProcessingService:
    provider = FakeLLM(_answer, embedder=_embedder())
    return ProcessingService(cluster_config, db, provider=provider, embedder=provider)


THREE = (
    "Первый пересказ события про VPN. Регулятор ограничил доступ. Подробности в тексте.",
    "Второй пересказ того же события другими словами. Сервисы заблокированы. Иначе.",
    "Совсем другая новость про рекомендательный сервис. Бета-версия. Квартал.",
)


def test_after_a_run_both_paraphrases_carry_a_proposal_and_the_singleton_does_not(service, db):
    _documents(db, *THREE)

    report = service.run(limit=10)

    assert report.items_new == 3  # S1 их не схлопнул: векторы документов ортогональны
    assert report.duplicates_proposed == 2
    cards = {row["summary"].split(":")[0]: int(row["id"]) for row in db.items.list(limit=10)}
    a1, a2, b = cards["Группа А"], None, cards["Группа Б"]
    ids = [int(row["id"]) for row in db.items.list(limit=10)]
    a_ids = sorted(i for i in ids if i != b)
    a1, a2 = a_ids
    for item_id, partner in ((a1, a2), (a2, a1)):
        revision = db.items.last_revision(item_id, DUPLICATE_FIELD)
        assert revision.source_of_change == "llm" and revision.actor == "model"
        payload = json.loads(revision.new_value)
        assert payload["items"] == [partner]
        assert payload["similarity"] > 0.9
        assert payload["run_id"] == db.processing_runs.latest().id
    assert db.items.last_revision(b, DUPLICATE_FIELD) is None


def test_the_card_exposes_the_proposal_with_partner_titles(service, db):
    _documents(db, *THREE)
    service.run(limit=10)
    items = ItemService(service.config, db)
    a1 = next(int(r["id"]) for r in db.items.list(limit=10) if "Группа А:" in r["summary"])

    proposal = items.get_item(a1)["duplicate_proposal"]

    assert proposal is not None
    assert [p["title"] for p in proposal["items"]] and proposal["similarity"] > 0.9
    assert proposal["items"][0]["id"] != a1
    b = next(int(r["id"]) for r in db.items.list(limit=10) if "Группа Б" in r["summary"])
    assert items.get_item(b)["duplicate_proposal"] is None


def test_the_same_proposal_is_not_written_twice(service, db):
    _documents(db, *THREE)
    service.run(limit=10)
    _documents(db, "Ещё одна новость про сервис. Ничего общего. Точка.")

    report = service.run(limit=10)

    assert report.items_new == 1
    a_ids = [int(r["id"]) for r in db.items.list(limit=10) if "Группа А" in r["summary"]]
    revisions = [r for i in a_ids for r in db.items.revisions(i) if r.field == DUPLICATE_FIELD]
    assert len(revisions) == 2  # по одному на карточку, не по одному на прогон
    assert report.duplicates_proposed == 0


def test_a_dismissed_pair_is_not_proposed_again(service, db):
    _documents(db, *THREE)
    service.run(limit=10)
    items = ItemService(service.config, db)
    a1, a2 = sorted(int(r["id"]) for r in db.items.list(limit=10) if "Группа А" in r["summary"])

    result = items.dismiss_duplicate(a1)

    assert result == {"id": a1, "dismissed": [a2]}
    assert items.duplicate_proposal(a1) is None and items.duplicate_proposal(a2) is None
    with db.transaction():
        assert items.propose_duplicate(a1, [a2], similarity=0.95, run_id=9) is False
        assert items.propose_duplicate(a1, [a2, 999], similarity=0.95, run_id=9) is True


def test_npa_cards_are_never_clustered(cluster_config, db, frozen_clock):
    provider = FakeLLM(_npa_answer, embedder=_embedder())
    service = ProcessingService(cluster_config, db, provider=provider, embedder=provider)
    _documents(db, THREE[0])
    service.run(limit=10)
    # второй акт с тем же саммари, но другим ключом — по похожести их объединять нельзя
    provider.answers = [lambda prompt: {**_npa_answer(prompt), "npa_key": "445566-8"}]
    _documents(db, THREE[1])

    report = service.run(limit=10)

    assert db.items.count() == 2 and report.duplicates_proposed == 0
    for row in db.items.list(limit=10):
        assert row["npa_key"] and db.items.last_revision(int(row["id"]), DUPLICATE_FIELD) is None


def test_a_clustering_failure_does_not_fail_the_run_but_is_logged(
    cluster_config, db, frozen_clock, caplog
):
    provider = FakeLLM(_answer, embedder=_embedder(fail_on_summaries=True))
    service = ProcessingService(cluster_config, db, provider=provider, embedder=provider)
    _documents(db, *THREE)

    with caplog.at_level(logging.ERROR, logger="processing"):
        report = service.run(limit=10)

    assert report.items_new == 3 and report.duplicates_proposed == 0
    assert db.processing_runs.latest().status == "done"
    assert any("кластеризация карточек не удалась" in r.getMessage() for r in caplog.records)


def test_clustering_can_be_switched_off(raw_config, db, frozen_clock):
    raw_config["embeddings"] = {"provider": "local", "dimensions": 16}
    raw_config["clustering"] = {"enabled": False}
    provider = FakeLLM(_answer, embedder=_embedder())
    service = ProcessingService(
        Config.from_dict(raw_config), db, provider=provider, embedder=provider
    )
    _documents(db, *THREE)

    report = service.run(limit=10)

    assert report.items_new == 3 and report.duplicates_proposed == 0
    assert all(len(texts) == 3 for texts in provider.embedded)  # только S1, саммари не считались


def test_the_summaries_are_embedded_once_per_run_as_one_batch(service, db):
    _documents(db, *THREE)
    service.run(limit=10)
    batches = [texts for texts in service.provider.embedded if any("Группа" in t for t in texts)]
    assert len(batches) == 1 and len(batches[0]) == 3


def test_degraded_cards_are_not_clustered(cluster_config, db, frozen_clock):
    """Без модели саммари — это лид страницы, а тип неизвестен: такие карточки в пул не идут."""
    embedder = FakeLLM(embedder=_embedder())
    service = ProcessingService(cluster_config, db, provider=None, embedder=embedder)
    _documents(db, *THREE)

    report = service.run(limit=10)

    assert report.items_new == 3 and report.degraded == 3
    assert report.duplicates_proposed == 0
    assert db.items.clustering_pool(since=None, limit=10) == []


def test_recluster_proposes_over_the_whole_window_after_settings_change(
    raw_config, db, frozen_clock
):
    """Прогон с выключенной кластеризацией ничего не предложил; `dedup` наверстывает."""
    raw_config["embeddings"] = {"provider": "local", "dimensions": 16}
    raw_config["clustering"] = {"enabled": False}
    provider = FakeLLM(_answer, embedder=_embedder())
    ProcessingService(Config.from_dict(raw_config), db, provider=provider, embedder=provider).run(
        limit=10
    ) if _documents(db, *THREE) else None
    assert (
        db.conn.execute(
            "SELECT count(*) FROM item_revisions WHERE field=?", (DUPLICATE_FIELD,)
        ).fetchone()[0]
        == 0
    )

    raw_config["clustering"] = {"min_cluster_size": 2, "min_samples": 1, "reduction": "none"}
    service = ProcessingService(
        Config.from_dict(raw_config), db, provider=provider, embedder=provider
    )

    assert service.recluster() == 2
    assert service.recluster() == 0  # то же предложение второй раз не пишется


def test_dedup_command_without_an_embedder_writes_nothing(hub_paths, file_db, capsys, monkeypatch):
    from src import cli

    monkeypatch.setattr(cli, "DEFAULT_PATHS", hub_paths)
    assert cli.main(["dedup"]) == 0
    assert "предложений «вероятный дубль»: 0" in capsys.readouterr().out
