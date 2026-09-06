"""Локальные эмбеддинги — единственный модуль, который импортирует `sentence_transformers`.

Наружу торчит тот же `EmbeddingProvider`, что и у облачного провайдера в `llm.py`,
поэтому сервис обработки не знает, откуда пришёл вектор (принцип I: провайдер
спрятан за протоколом). Модель грузится лениво и один раз на процесс: в HTTP-
процессе она общая для всех прогонов, в CLI — живёт столько, сколько команда.

Ошибки разделены так же, как у модели: не загрузилась (нет файлов, кривой
конфиг, неизвестное устройство) — `LlmConfigError`, прогон останавливается
громко; не посчиталась (нехватка памяти на ускорителе) — `LlmTemporaryError`,
S1 в этом прогоне идёт по URL и SimHash, и об этом пишется ошибка в лог.

Векторы всегда нормируются по L2 здесь, а не там, где сравниваются: косинус
двух нормированных векторов — это их скалярное произведение, и дальше по
коду нет ни одного места, которое зависело бы от нормы.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..config import EmbeddingsConfig, LLMConfig
from ..utils import get_logger
from .llm import EmbeddingProvider, LlmConfigError, LlmTemporaryError, OllamaProvider

log = get_logger("embeddings")


def _torch():
    import torch  # импортируется здесь: тесты без модели не должны тянуть torch

    return torch


def select_device(requested: str = "auto", torch_module: Any = None) -> str:
    """`cuda`, затем `mps` (Apple Silicon), иначе `cpu`; явный выбор не оспаривается."""
    if requested and requested != "auto":
        return requested
    torch = torch_module or _torch()
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def select_dtype(requested: str, device: str, torch_module: Any = None):
    """bfloat16 там, где ускоритель его умеет; на CPU — float32 (bf16 там медленнее)."""
    torch = torch_module or _torch()
    explicit = {
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }
    if requested in explicit:
        return explicit[requested]
    if device == "cuda":
        supported = getattr(torch.cuda, "is_bf16_supported", lambda: True)()
        return torch.bfloat16 if supported else torch.float16
    if device == "mps":
        return torch.bfloat16
    return torch.float32


def _dtype_keyword() -> str:
    """`dtype` в transformers >= 4.56, `torch_dtype` — в более старых."""
    try:
        import transformers

        major, minor = (int(part) for part in transformers.__version__.split(".")[:2])
    except (ImportError, ValueError):
        return "torch_dtype"
    return "dtype" if (major, minor) >= (4, 56) else "torch_dtype"


def load_sentence_transformer(config: EmbeddingsConfig, device: str):
    """Собрать `SentenceTransformer` под конфиг. Без flash_attention_2: он только CUDA."""
    from sentence_transformers import SentenceTransformer

    torch = _torch()
    dtype = select_dtype(config.dtype, device, torch)
    model = SentenceTransformer(
        config.model,
        device=device,
        trust_remote_code=True,
        model_kwargs={_dtype_keyword(): dtype, "trust_remote_code": True},
        config_kwargs={"trust_remote_code": True},
        tokenizer_kwargs={"trust_remote_code": True},
    )
    model.max_seq_length = config.max_seq_length
    return model


@dataclass
class LocalEmbeddingProvider:
    """Модель sentence-transformers на этой машине за протоколом `EmbeddingProvider`.

    `loader` подменяется в тестах: настоящая модель весит гигабайты и в тестах
    не нужна — важно, что провайдер нормирует, проверяет длину и переводит
    ошибки в те же классы, что и облачный.
    """

    config: EmbeddingsConfig
    loader: Callable[[EmbeddingsConfig, str], Any] = load_sentence_transformer
    device: str = ""  # решается при загрузке; пусто — ещё не грузили
    _model: Any = field(default=None, init=False, repr=False)
    # Провайдер один на процесс API; ленивую загрузку из двух потоков нельзя
    # пускать наперегонки — две копии модели в памяти не влезут.
    _lock: Any = field(default_factory=threading.Lock, init=False, repr=False)

    @property
    def model_name(self) -> str:
        return self.config.model

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                device = self.device or select_device(self.config.device)
                started = time.monotonic()
                try:
                    model = self.loader(self.config, device)
                except Exception as e:  # ImportError, OSError, ValueError, RuntimeError — конфиг
                    raise LlmConfigError(
                        f"локальная модель эмбеддингов «{self.config.model}» не загрузилась "
                        f"на {device}: {type(e).__name__}: {e}"
                    ) from e
                self.device = device
                self._model = model
                log.info(
                    "модель эмбеддингов %s загружена на %s за %.1f с",
                    self.config.model,
                    device,
                    time.monotonic() - started,
                )
        return self._model

    def embed_tensor(self, texts: Sequence[str]):
        """Нормированные векторы одним тензором float32 на CPU, форма N × D."""
        torch = _torch()
        if not texts:
            return torch.empty(0, 0, dtype=torch.float32)
        model = self._load()
        options: dict = {}
        if self.config.prompt:
            options["prompt"] = self.config.prompt
        try:
            with torch.inference_mode():
                raw = model.encode(
                    list(texts),
                    batch_size=self.config.batch_size,
                    convert_to_tensor=True,
                    normalize_embeddings=False,  # нормируем сами, ниже, одинаково для всех
                    show_progress_bar=False,
                    **options,
                )
        except (RuntimeError, MemoryError) as e:  # нехватка памяти ускорителя, сбой ядра
            raise LlmTemporaryError(
                f"эмбеддинги не посчитались на {self.device}: {type(e).__name__}: {e}"
            ) from e
        tensor = torch.as_tensor(raw).detach().to("cpu", torch.float32)
        if tensor.ndim == 1:
            tensor = tensor.unsqueeze(0)
        tensor = torch.nn.functional.normalize(tensor, p=2, dim=-1)
        self._check_width(tensor.shape[1] if tensor.ndim == 2 else 0)
        return tensor

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed_tensor(texts).tolist()

    def _check_width(self, width: int) -> None:
        expected = self.config.dimensions
        if expected and width != expected:
            raise LlmConfigError(
                f"модель «{self.config.model}» вернула векторы длины {width}, а "
                f"embeddings.dimensions = {expected}: поправьте конфиг — иначе старые и "
                "новые векторы в базе несравнимы"
            )

    def close(self) -> None:
        self._model = None


def build_embedder(
    embeddings: EmbeddingsConfig,
    llm: LLMConfig,
    api_key: str = "",
    *,
    shared: OllamaProvider | None = None,
) -> EmbeddingProvider | None:
    """Провайдер эмбеддингов по `embeddings.provider`.

    `local` не требует ни ключа, ни сети после первой загрузки модели. `ollama` —
    прежний облачный путь, включается явно; без ключа он не собирается, и об этом
    говорится ошибкой, а не тишиной. `off` — эмбеддингов нет по решению
    оператора: S1 работает по URL и SimHash.
    """
    if embeddings.provider == "off":
        log.warning(
            "embeddings.provider = off: S1 идёт без эмбеддингов, перепечатки другими "
            "словами не схлопываются"
        )
        return None
    if embeddings.provider == "ollama":
        if shared is not None:
            return shared
        if not api_key:
            log.error(
                "embeddings.provider = ollama, но ключа %s нет: эмбеддингов не будет, "
                "S1 идёт только по URL и SimHash",
                llm.api_key_env,
            )
            return None
        return OllamaProvider(config=llm, api_key=api_key)
    return LocalEmbeddingProvider(embeddings)


def as_tensor(provider: EmbeddingProvider, texts: Sequence[str]):
    """Нормированные векторы как тензор от любого провайдера.

    Локальный отдаёт тензор сразу; облачный или подменный — список, который
    здесь превращается в тензор и нормируется тем же способом. Дальше по
    кластеризации вектора живут только как torch-тензоры: раскодировали один раз.
    """
    torch = _torch()
    method = getattr(provider, "embed_tensor", None)
    if callable(method):
        return method(texts)
    vectors = provider.embed(texts) if texts else []
    if not vectors:
        return torch.empty(0, 0, dtype=torch.float32)
    tensor = torch.tensor(vectors, dtype=torch.float32)
    return torch.nn.functional.normalize(tensor, p=2, dim=-1)
