"""Вторая дедупликация: группы карточек-дублей по эмбеддингам саммари.

S1 работает до модели и экономит вызовы на перепечатках; здесь — после модели,
по уже созданным карточкам: те, что S1 пропустил (пересказ другими словами,
разные заголовки), находятся по близости саммари. Шаги, как в постановке:

1. векторы саммари свежих карточек (плюс карточки окна, чтобы было с чем сравнить);
2. нормировка по L2 — косинус равен скалярному произведению;
3. снижение размерности: PCA на torch (без новых зависимостей), UMAP по желанию;
4. HDBSCAN в евклидовой метрике по нормированным строкам: на единичной сфере
   ‖a − b‖² = 2 − 2·cos(a, b), так что это косинусное пространство без фиксированного
   порога; одиночки — шум (label = −1), их не трогают.

Одна поправка к HDBSCAN «из коробки», видная в калибровке (`quality --pairs`):
у него плотность относительная — любая пара взаимных ближайших соседей для
него кластер, а к плотной группе он приписывает всё, что «уронил» по дороге к
её ядру. Поэтому внутри каждого кластера остаются только группы полной связи
(каждая пара ≥ `min_similarity`) в исходном пространстве. Зона «вероятный
дубль» — от `min_similarity` до `processing.cosine_threshold`, выше S1
склеивает сам.

Ни один вектор здесь не превращается обратно в список: матрица — один тензор
на прогон, нормы посчитаны один раз. HDBSCAN получает представление того же
буфера через numpy, копий нет.

Кластер — не склейка: результат этого модуля — предложение «вероятный дубль»,
которое сервис записывает как ревизию модели, а объединяет — аналитик.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from ..config import ClusteringConfig
from ..utils import get_logger

log = get_logger("clustering")

NOISE = -1
# UMAP не имеет смысла на горстке точек: соседей меньше, чем он просит; на
# таком объёме PCA даёт то же, что и отсутствие снижения.
_UMAP_MIN_POINTS = 10


@dataclass(frozen=True)
class DuplicateGroup:
    """Карточки одного кластера и их среднее попарное сходство (по исходным векторам)."""

    item_ids: tuple[int, ...]
    similarity: float
    label: int

    def partners(self, item_id: int) -> list[int]:
        return [other for other in self.item_ids if other != item_id]


def normalize(matrix):
    """Строки к единичной длине; нулевые строки остаются нулевыми, а не NaN."""
    import torch

    return torch.nn.functional.normalize(matrix.to(torch.float32), p=2, dim=-1)


def reduce(matrix, method: str = "pca", n_components: int = 32, seed: int = 0):
    """Снизить размерность строк матрицы; `none` или нулевое `n_components` — как есть.

    После PCA строки нормируются заново: тогда HDBSCAN в евклидовой метрике
    по-прежнему считает косинусы. UMAP отдаёт своё пространство, там евклид
    и есть родная метрика.
    """
    import torch

    points, width = matrix.shape
    if method == "none" or n_components <= 0 or points < 3:
        return matrix
    if method == "umap" and points < _UMAP_MIN_POINTS:
        log.info("кластеризация: %d точек мало для UMAP, берём PCA", points)
        method = "pca"
    if method == "umap":
        return _umap(matrix, n_components, seed)
    components = min(n_components, points - 1, width)
    if components >= width:
        return matrix
    centered = matrix - matrix.mean(dim=0, keepdim=True)
    # Точное SVD вместо `pca_lowrank`: на сотнях строк это доли секунды, зато
    # результат детерминирован и не зависит от начального приближения.
    _, _, vh = torch.linalg.svd(centered, full_matrices=False)
    projected = centered @ vh[:components].T
    return normalize(projected)


def _umap(matrix, n_components: int, seed: int):
    import torch

    try:
        import umap  # необязательная зависимость: `uv sync --extra umap`
    except ImportError as e:
        raise RuntimeError(
            "clustering.reduction = umap, но пакет umap-learn не установлен: "
            "`uv sync --extra umap` или reduction: pca"
        ) from e
    points = matrix.shape[0]
    reducer = umap.UMAP(
        n_components=min(n_components, points - 2),
        n_neighbors=min(15, points - 1),
        metric="cosine",
        min_dist=0.0,
        random_state=seed,
    )
    reduced = reducer.fit_transform(matrix.numpy())
    return torch.as_tensor(reduced, dtype=torch.float32)


def cluster_labels(matrix, config: ClusteringConfig) -> list[int]:
    """Метки HDBSCAN для строк матрицы; −1 — шум. Меньше трёх точек — все шум.

    `allow_single_cluster` включён: в пуле из одной пары дублей и одиночек
    дерево кластеров состоит из одного корня, и без него HDBSCAN не выделил бы
    ничего.
    """
    points = matrix.shape[0] if matrix.ndim == 2 else 0
    if points < max(3, config.min_cluster_size):
        return [NOISE] * points
    import hdbscan

    clusterer = hdbscan.HDBSCAN(
        min_cluster_size=config.min_cluster_size,
        min_samples=config.min_samples,
        metric="euclidean",  # на нормированных строках это косинусное расстояние
        cluster_selection_method=config.cluster_selection_method,
        cluster_selection_epsilon=float(config.cluster_selection_epsilon),
        allow_single_cluster=True,
    )
    labels = clusterer.fit_predict(matrix.to("cpu").contiguous().numpy().astype("float64"))
    return [int(label) for label in labels]


def tight_groups(rows, floor: float) -> list[list[int]]:
    """Группы, в которых каждая пара строк похожа не ниже `floor` (полная связь).

    Кластер HDBSCAN — кандидат: он тянет за собой всё, что «уронил» по дороге к
    ядру, и склеивает соседние группы через слабую перемычку. Связные компоненты
    по одиночным рёбрам здесь не годятся — они цепляются: A~B 0.72, B~C 0.71,
    C~D 0.70, и одиннадцать заметок об одних переговорах становятся «дублями».
    Полная связь склеивает две группы, только если все пары между ними ≥ floor;
    сливаются сначала самые похожие.
    """
    count = rows.shape[0]
    if count == 0:
        return []
    sims = (rows @ rows.T).tolist()
    groups: list[list[int]] = [[index] for index in range(count)]
    while True:
        best: tuple[float, int, int] | None = None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                score = min(sims[i][j] for i in groups[a] for j in groups[b])
                if score >= floor and (best is None or score > best[0]):
                    best = (score, a, b)
        if best is None:
            break
        _, a, b = best
        groups[a] = groups[a] + groups[b]
        del groups[b]
    return [sorted(group) for group in groups]


def mean_similarity(rows) -> float:
    """Среднее попарное сходство строк (нормированных): один matmul, без циклов."""
    count = rows.shape[0]
    if count < 2:
        return 1.0
    sims = rows @ rows.T
    total = float(sims.sum()) - float(sims.diagonal().sum())
    return total / (count * (count - 1))


def find_groups(item_ids: Sequence[int], matrix, config: ClusteringConfig) -> list[DuplicateGroup]:
    """Группы вероятных дублей среди карточек: нормировка → снижение → HDBSCAN.

    `matrix` — по строке на карточку в порядке `item_ids`. Сходство группы
    считается по исходным нормированным векторам, а не по сниженным: это
    число показывают аналитику, и оно должно быть тем же косинусом, что в S1.
    """
    if len(item_ids) == 0 or matrix.ndim != 2 or matrix.shape[0] != len(item_ids):
        return []
    unit = normalize(matrix)
    reduced = reduce(unit, config.reduction, config.n_components, config.seed)
    labels = cluster_labels(reduced, config)
    members: dict[int, list[int]] = {}
    for position, label in enumerate(labels):
        if label != NOISE:
            members.setdefault(label, []).append(position)
    groups: list[DuplicateGroup] = []
    for label in sorted(members):
        positions = members[label]
        if len(positions) < 2:
            continue
        # У HDBSCAN плотность относительная: два взаимных ближайших соседа — уже
        # кластер, даже если между ними косинус 0.3. Внутри кластера остаются
        # только группы полной связи по порогу снизу — остальное не дубль, и
        # спрашивать аналитика о нём незачем.
        for group in tight_groups(unit[positions], config.min_similarity):
            if len(group) < 2:
                continue
            chosen = [positions[index] for index in group]
            groups.append(
                DuplicateGroup(
                    item_ids=tuple(int(item_ids[p]) for p in chosen),
                    similarity=round(mean_similarity(unit[chosen]), 4),
                    label=label,
                )
            )
    return groups
