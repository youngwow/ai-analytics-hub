"""src/processing/embeddings.py — локальные эмбеддинги за тем же протоколом, что и облако.

Настоящая модель весит гигабайты и в тестах не грузится: провайдеру подставляется
`loader`, который возвращает поддельный `SentenceTransformer`. Проверяется то, за
что отвечает сам провайдер — выбор устройства и типа, нормировка, проверка
длины, перевод ошибок в `LlmConfigError` / `LlmTemporaryError` — и то, как S1
ведёт себя без эмбеддингов: громко, а не молча.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
import torch
from support import ARTICLE_TEXT, NEWS_TEXT, FakeLLM, news_answer

from src.config import Config, EmbeddingsConfig, LLMConfig
from src.models import RawDocument, Source
from src.processing import embeddings as embeddings_mod
from src.processing.dedup import encode_vector, simhash
from src.processing.embeddings import (
    LocalEmbeddingProvider,
    as_tensor,
    build_embedder,
    select_device,
    select_dtype,
)
from src.processing.llm import LlmConfigError, LlmTemporaryError, OllamaProvider
from src.services.processing_service import ProcessingService

NOW = "2026-09-02T12:00:00+00:00"


def _torch(*, cuda: bool = False, mps: bool = False, bf16: bool = True):
    """Поддельный torch: только то, что читает выбор устройства и типа."""
    return SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda, is_bf16_supported=lambda: bf16),
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)),
        float32="float32",
        float16="float16",
        bfloat16="bfloat16",
    )


class FakeModel:
    """`SentenceTransformer` без модели: отдаёт ненормированные векторы известной длины."""

    def __init__(self, width: int = 4, fail: Exception | None = None):
        self.width = width
        self.fail = fail
        self.calls: list[tuple[list[str], dict]] = []
        self.max_seq_length = None

    def encode(self, texts, **options):
        self.calls.append((list(texts), options))
        if self.fail is not None:
            raise self.fail
        row = [3.0, 4.0] + [0.0] * (self.width - 2)  # норма 5 → после нормировки 0.6, 0.8
        return torch.tensor([row] * len(texts))


def _provider(width: int = 4, dims: int = 4, **overrides) -> tuple[LocalEmbeddingProvider, list]:
    loads: list[tuple[EmbeddingsConfig, str]] = []
    model = FakeModel(width=width, fail=overrides.pop("fail", None))

    def loader(config, device):
        loads.append((config, device))
        return model

    config = EmbeddingsConfig(dimensions=dims, device="cpu", **overrides)
    return LocalEmbeddingProvider(config, loader=loader), loads


# ── устройство и тип ───────────────────────────────────────────────────────


def test_cuda_wins_over_mps_and_cpu():
    assert select_device("auto", _torch(cuda=True, mps=True)) == "cuda"


def test_apple_silicon_gets_mps():
    assert select_device("auto", _torch(mps=True)) == "mps"


def test_without_an_accelerator_the_device_is_cpu():
    assert select_device("auto", _torch()) == "cpu"


def test_an_explicit_device_is_not_second_guessed():
    assert select_device("cpu", _torch(cuda=True)) == "cpu"


@pytest.mark.parametrize(
    ("requested", "device", "fake", "expected"),
    [
        ("auto", "mps", _torch(mps=True), "bfloat16"),
        ("auto", "cuda", _torch(cuda=True), "bfloat16"),
        ("auto", "cuda", _torch(cuda=True, bf16=False), "float16"),
        ("auto", "cpu", _torch(), "float32"),
        ("float32", "mps", _torch(mps=True), "float32"),
        ("float16", "cpu", _torch(), "float16"),
    ],
    ids=["mps-bf16", "cuda-bf16", "cuda-no-bf16", "cpu-f32", "explicit-f32", "explicit-f16"],
)
def test_dtype_is_bfloat16_where_the_accelerator_supports_it(requested, device, fake, expected):
    assert select_dtype(requested, device, fake) == expected


# ── провайдер ──────────────────────────────────────────────────────────────


def test_vectors_are_l2_normalised_before_they_leave_the_provider():
    provider, _ = _provider()

    vectors = provider.embed(["один", "два"])

    assert len(vectors) == 2
    assert vectors[0] == pytest.approx([0.6, 0.8, 0.0, 0.0])
    assert sum(x * x for x in vectors[1]) == pytest.approx(1.0)


def test_embed_tensor_is_float32_on_cpu_with_unit_rows():
    provider, _ = _provider()

    tensor = provider.embed_tensor(["один", "два", "три"])

    assert tensor.shape == (3, 4)
    assert tensor.dtype == torch.float32
    assert tensor.device.type == "cpu"
    assert torch.allclose(tensor.norm(dim=1), torch.ones(3))


def test_the_model_is_loaded_lazily_and_only_once():
    provider, loads = _provider()
    assert loads == []  # конструктор ничего не грузит

    provider.embed(["один"])
    provider.embed(["два"])

    assert len(loads) == 1
    assert loads[0][1] == "cpu"
    assert provider.device == "cpu"


def test_empty_input_does_not_load_the_model():
    provider, loads = _provider()
    assert provider.embed([]) == []
    assert loads == []


def test_the_prompt_prefix_is_passed_to_encode_only_when_configured():
    provider, _ = _provider(prompt="Instruct: Retrieve similar news\nQuery: ")
    provider.embed(["текст"])
    assert provider._model.calls[0][1]["prompt"].startswith("Instruct:")

    plain, _ = _provider(prompt="")
    plain.embed(["текст"])
    assert "prompt" not in plain._model.calls[0][1]


def test_the_default_prompt_is_the_same_event_instruction():
    assert EmbeddingsConfig().prompt.startswith(
        "Instruct: Retrieve news that report the same event"
    )


def test_a_vector_of_the_wrong_width_is_a_configuration_error():
    provider, _ = _provider(width=4, dims=2048)
    with pytest.raises(LlmConfigError, match="embeddings.dimensions = 2048"):
        provider.embed(["текст"])


def test_dimensions_zero_disables_the_width_check():
    provider, _ = _provider(width=4, dims=0)
    assert len(provider.embed(["текст"])[0]) == 4


def test_a_model_that_fails_to_load_stops_the_run_as_a_config_error():
    config = EmbeddingsConfig(model="nope/none", device="cpu")

    def loader(config, device):
        raise OSError("no such repository")

    provider = LocalEmbeddingProvider(config, loader=loader)
    with pytest.raises(LlmConfigError, match="nope/none"):
        provider.embed(["текст"])


def test_an_accelerator_failure_is_temporary():
    provider, _ = _provider(fail=RuntimeError("MPS backend out of memory"))
    with pytest.raises(LlmTemporaryError, match="out of memory"):
        provider.embed(["текст"])


def test_close_drops_the_model_so_the_next_call_reloads_it():
    provider, loads = _provider()
    provider.embed(["текст"])
    provider.close()
    provider.embed(["текст"])
    assert len(loads) == 2


# ── фабрика ────────────────────────────────────────────────────────────────


def test_off_means_no_embedder_and_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="embeddings"):
        assert build_embedder(EmbeddingsConfig(provider="off"), LLMConfig()) is None
    assert "provider = off" in caplog.text


def test_local_needs_no_key():
    embedder = build_embedder(EmbeddingsConfig(provider="local"), LLMConfig(), api_key="")
    assert isinstance(embedder, LocalEmbeddingProvider)
    assert embedder.model_name == "ai-sage/Giga-Embeddings-instruct"


def test_ollama_reuses_the_shared_provider_or_builds_its_own():
    llm = LLMConfig()
    shared = OllamaProvider(config=llm, api_key="k")
    assert build_embedder(EmbeddingsConfig(provider="ollama"), llm, "k", shared=shared) is shared
    own = build_embedder(EmbeddingsConfig(provider="ollama"), llm, "k")
    assert isinstance(own, OllamaProvider) and own is not shared


def test_ollama_without_a_key_is_an_error_not_silence(caplog):
    with caplog.at_level(logging.ERROR, logger="embeddings"):
        assert build_embedder(EmbeddingsConfig(provider="ollama"), LLMConfig(), "") is None
    assert "OLLAMA_API_KEY" in caplog.text


def test_as_tensor_normalises_lists_from_any_provider():
    fake = FakeLLM(embedder=lambda texts: [[3.0, 4.0] for _ in texts])

    tensor = as_tensor(fake, ["a", "b"])

    assert tensor.shape == (2, 2)
    assert torch.allclose(tensor[0], torch.tensor([0.6, 0.8]))


def test_as_tensor_prefers_the_provider_tensor():
    provider, _ = _provider()
    assert as_tensor(provider, ["a"]).shape == (1, 4)
    assert as_tensor(FakeLLM(), []).shape == (0, 0)


def test_the_real_loader_asks_for_bfloat16_without_flash_attention(monkeypatch):
    """Сборка `SentenceTransformer`: bf16 на ускорителе, trust_remote_code, без flash_attention_2."""
    captured: dict = {}

    class Recorder:
        def __init__(self, name, **kwargs):
            captured["name"], captured["kwargs"] = name, kwargs

    monkeypatch.setitem(
        __import__("sys").modules,
        "sentence_transformers",
        SimpleNamespace(SentenceTransformer=Recorder),
    )
    model = embeddings_mod.load_sentence_transformer(
        EmbeddingsConfig(dtype="bfloat16", max_seq_length=777), "mps"
    )

    assert captured["name"] == "ai-sage/Giga-Embeddings-instruct"
    kwargs = captured["kwargs"]
    assert kwargs["device"] == "mps" and kwargs["trust_remote_code"] is True
    dtype_key = embeddings_mod._dtype_keyword()
    assert kwargs["model_kwargs"][dtype_key] == torch.bfloat16
    assert "attn_implementation" not in kwargs["model_kwargs"]
    assert model.max_seq_length == 777


# ── S1 без эмбеддингов: громко, а не молча ─────────────────────────────────


def _document(db, external_id: str, text: str, title: str = "") -> int:
    source = db.sources.get_by_fetch_url("https://a.ru/rss") or db.sources.add(
        Source(name="Лента", url="https://a.ru/", kind="rss", fetch_url="https://a.ru/rss")
    )
    with db.transaction():
        return db.documents.insert(
            RawDocument(
                source_id=source.id,
                external_id=external_id,
                url=f"https://a.ru/{external_id}",
                title=title or f"Материал {external_id}",
                text=text,
                published_at=NOW,
                fetched_at=NOW,
            )
        )


def test_a_temporary_embedding_failure_is_logged_as_an_error_and_the_run_goes_on(
    config, db, frozen_clock, caplog
):
    _document(db, "n1", NEWS_TEXT)
    broken = FakeLLM(news_answer(), embedder=LlmTemporaryError("MPS out of memory"))
    service = ProcessingService(config, db, provider=broken, embedder=broken)

    with caplog.at_level(logging.ERROR, logger="processing"):
        report = service.run(limit=5)

    assert report.items_new == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("только по URL и SimHash" in r.getMessage() for r in errors)


def test_a_misconfigured_embedding_model_stops_the_run(config, db, frozen_clock):
    _document(db, "n1", NEWS_TEXT)
    broken = FakeLLM(news_answer(), embedder=LlmConfigError("модель не загрузилась"))
    service = ProcessingService(config, db, provider=broken, embedder=broken)

    with pytest.raises(LlmConfigError):
        service.run(limit=5)

    assert db.items.count() == 0
    assert db.processing_runs.latest().status == "failed"


def test_a_missing_embedder_is_an_error_when_embeddings_are_configured(
    raw_config, db, frozen_clock, caplog
):
    raw_config["embeddings"] = {"provider": "local"}
    config = Config.from_dict(raw_config)
    _document(db, "n1", NEWS_TEXT)
    service = ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None)

    with caplog.at_level(logging.ERROR, logger="processing"):
        service.run(limit=5)

    assert any("провайдер эмбеддингов" in r.getMessage() for r in caplog.records)


def test_a_missing_embedder_is_silent_only_when_switched_off(config, db, frozen_clock, caplog):
    assert config.embeddings.provider == "off"
    _document(db, "n1", NEWS_TEXT)
    service = ProcessingService(config, db, provider=FakeLLM(news_answer()), embedder=None)

    with caplog.at_level(logging.ERROR, logger="processing"):
        service.run(limit=5)

    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


def test_a_paraphrased_reprint_joins_the_existing_card_by_cosine(
    config, db, item_factory, frozen_clock
):
    """Перепечатка другими словами: SimHash далёк, косинус по эмбеддингам близок."""
    carded = _document(db, "old", ARTICLE_TEXT)
    stored = [1.0, 0.0, 0.0, 0.0]
    with db.transaction():
        db.documents.set_derived(
            carded,
            simhash=simhash(ARTICLE_TEXT),
            embedding=encode_vector(stored),
            norm_text=ARTICLE_TEXT,
        )
    item_factory(db, carded, published_at=NOW)
    _document(db, "new", NEWS_TEXT)  # текст другой — SimHash его не поймает
    close = [0.97, 0.24, 0.0, 0.0]  # косинус к stored ≈ 0.97 > 0.86
    provider = FakeLLM(news_answer(), embedder=lambda texts: [close for _ in texts])
    raw = dict(config.raw)
    raw["embeddings"] = {"provider": "local", "dimensions": 4}
    service = ProcessingService(Config.from_dict(raw), db, provider=provider, embedder=provider)

    report = service.run(limit=5)

    assert (report.items_new, report.items_joined) == (0, 1)
    assert provider.calls == 0  # перепечатка не стоила вызова модели
    assert db.items.count() == 1


def test_close_handles_a_local_embedder_and_closes_a_shared_provider_once(config, db):
    """Провайдеры — dataclass'ы без хэша: `close()` не должен класть их в множество."""
    local, loads = _provider()
    local.embed(["текст"])
    shared = FakeLLM(news_answer())

    ProcessingService(config, db, provider=shared, embedder=local).close()
    assert local._model is None
    assert shared.closed == 1

    ProcessingService(config, db, provider=shared, embedder=shared).close()
    assert shared.closed == 2  # общий объект закрыт один раз, а не дважды


def test_both_documents_of_a_batch_pair_keep_their_vectors_when_the_longer_one_wins(
    config, db, frozen_clock
):
    """Перепечатка в том же прогоне: канонический документ меняется на более полный,
    но прежний не должен терять simhash и вектор — иначе он выпадает из кандидатов."""
    short = _document(db, "short", "Минцифры внесло законопроект. Коротко.")
    long = _document(db, "long", NEWS_TEXT + " " + ARTICLE_TEXT)
    vector = [0.6, 0.8, 0.0, 0.0]
    provider = FakeLLM(news_answer(), embedder=lambda texts: [vector for _ in texts])
    raw = dict(config.raw)
    raw["embeddings"] = {"provider": "local", "dimensions": 4}
    service = ProcessingService(Config.from_dict(raw), db, provider=provider, embedder=provider)

    report = service.run(limit=5)

    assert (report.items_new, report.clusters) == (1, 1)
    for document_id in (short, long):
        row = db.conn.execute(
            "SELECT simhash, embedding FROM documents WHERE id=?", (document_id,)
        ).fetchone()
        assert row["simhash"] and row["embedding"], f"документ #{document_id} потерял S1-поля"
    canonical = next(s for s in db.items.sources(1) if s["is_canonical"])
    assert canonical["id"] == long
