"""Золотой набор дедупликации на настоящей модели эмбеддингов.

Медленный тест: грузит `ai-sage/Giga-Embeddings-instruct` из локального кэша
Hugging Face (~14 ГБ на диске, ~7 ГБ в памяти) и прогоняет пары «дубль / не
дубль» через те же функции, что `python -m src quality --pairs`. Без кэша —
пропуск, а не падение: сеть тестам не положена.

Приёмка Feature 1: перепечатка другими словами не даёт отдельной карточки — то
есть каждая пара-дубль проходит порог S1, а ни одна пара «та же тема, другое
событие» его не проходит. Приёмка Feature 2: те же пары в общем пуле HDBSCAN
находит группами, одиночки остаются шумом.
"""

from __future__ import annotations

import pytest

from src.config import Config
from src.paths import DEFAULT_PATHS
from src.processing.embeddings import LocalEmbeddingProvider
from src.processing.quality import evaluate_pairs, load_pairs

pytestmark = pytest.mark.slow


def _cached_snapshot() -> str | None:
    try:
        from huggingface_hub import snapshot_download

        return snapshot_download("ai-sage/Giga-Embeddings-instruct", local_files_only=True)
    except Exception:  # нет кэша, нет пакета — нет и теста
        return None


@pytest.fixture(scope="module")
def shipped() -> Config:
    return Config.load(DEFAULT_PATHS.config_path)


@pytest.fixture(scope="module")
def result(shipped):
    if _cached_snapshot() is None:
        pytest.skip("модель ai-sage/Giga-Embeddings-instruct не скачана в кэш Hugging Face")
    pairs = load_pairs()
    assert pairs, "набор пар пуст"
    provider = LocalEmbeddingProvider(shipped.embeddings)
    try:
        return evaluate_pairs(
            provider,
            pairs,
            clustering=shipped.clustering,
            threshold=shipped.processing.cosine_threshold,
        )
    finally:
        provider.close()


def test_every_paraphrase_passes_s1_and_no_same_topic_pair_does(result):
    s1 = result["s1"]
    assert s1["recall"] == 1.0, f"дубли ниже порога S1: {s1}"
    assert s1["precision"] == 1.0, f"не-дубли выше порога S1: {s1}"
    assert s1["max_non_duplicate"] < s1["threshold"] < s1["min_duplicate"]


def test_the_shipped_clustering_finds_every_pair_and_few_false_groups(result):
    """Порог снизу 0.7 выбран по реальным саммари (дубли одного события — 0.73);
    на рукописном наборе он пропускает несколько пар «та же тема» — это цена полноты."""
    current = next(row for row in result["clustering"] if row.get("current"))
    assert current["recall"] == 1.0, current
    assert current["precision"] >= 0.75, current
    assert not [miss for miss in result["misses"] if miss.startswith("пропущен")], result["misses"]
