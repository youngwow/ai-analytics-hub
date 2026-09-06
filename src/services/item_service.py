"""ItemService — карточки после обработки: правка, видимость, заметки, история.

Отделён от `ProcessingService` по одному признаку — зовёт ли метод модель.
Здесь только база, поэтому сервис строится без провайдера и одинаково служит
CLI и HTTP (принцип III конституции).
"""

from __future__ import annotations

import json

from ..config import Config
from ..exceptions import (
    ItemNotFoundError,
    ItemValidationError,
    NoDuplicateProposalError,
    NothingToRevertError,
)
from ..models import PRIORITIES, Item, ItemNote, ItemRevision, NpaEvent
from ..repositories import Database
from ..utils import get_logger, parse_datetime, to_utc_iso, utc_now

log = get_logger("items")

# Предложение «вероятный дубль» живёт в истории как ревизия модели по этому полю —
# тот же приём, что и у версий саммари/приоритета (`model_proposals`): без новой
# таблицы и без миграции. Ревизия человека по тому же полю (merge / not_duplicate)
# закрывает предложение; поглощённая карточка получает `merged_into`.
DUPLICATE_FIELD = "duplicate_of"
MERGED_FIELD = "merged_into"

# `analyst_note` сюда не входит с v8: заметка живёт в `item_notes` (add_note),
# а не в колонке карточки — один источник правды.
EDITABLE_FIELDS = ("title", "summary", "priority", "type", "tags", "npa_status")
# Fields a human may edit and therefore may want to revert to the model's version.
REVERTIBLE_FIELDS = ("title", "summary", "priority", "type", "tags")
# A later publication may only move a bill forward; a retrospective article must
# not drag a card back to "анонс".
_STATUS_ORDER = ("анонс", "разработка", "внесён", "рассмотрение", "принят", "действует", "архив")


class ItemService:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db

    # -- reading --

    def list_items(self, **filters) -> list:
        return self.db.items.list(**filters)

    def get_item(self, item_id: int) -> dict:
        """Карточка целиком: сущности, источники, хронология, ревизии, заметки, теги.

        Строки источников отдаются словарями — дальше сервиса `sqlite3.Row` не идёт.
        """
        item = self._require(item_id)
        return {
            "item": item,
            "entities": self.db.items.entities(item_id),
            "sources": [dict(r) for r in self.db.items.sources(item_id)],
            "events": self.db.items.events(item_id),
            "revisions": self.db.items.revisions(item_id),
            "notes": self.db.notes.list(item_id),
            "tags": self.db.tags.list(item_id),
            "model_proposals": {
                name: self.db.items.last_model_value(item_id, name)
                for name in REVERTIBLE_FIELDS
            },
            "duplicate_proposal": self.duplicate_proposal(item_id),
        }

    def revisions(self, item_id: int) -> list[ItemRevision]:
        """История правок человека и модели (US-8)."""
        self._require(item_id)
        return self.db.items.revisions(item_id)

    def _require(self, item_id: int) -> Item:
        item = self.db.items.get(item_id)
        if item is None:
            raise ItemNotFoundError(f"карточка #{item_id} не найдена")
        return item

    # -- editing --

    def edit_item(
        self, item_id: int, fields: dict, actor: str = "user", reason: str = ""
    ) -> Item:
        """Apply an analyst's edit: mark the field, keep the before/after in history."""
        item = self.db.items.get(item_id)
        if item is None:
            raise ItemNotFoundError(f"карточка #{item_id} не найдена")
        changed: list[ItemRevision] = []
        for name, value in fields.items():
            if value is None or name not in EDITABLE_FIELDS:
                continue
            if name in ("title", "summary") and not str(value).strip():
                # Пустое саммари — не способ спрятать карточку, для этого есть hide.
                raise ItemValidationError(f"поле «{name}» не может быть пустым")
            old = getattr(item, name)
            if old == value:
                continue
            changed.append(
                ItemRevision(
                    item_id=item_id,
                    field=name,
                    old_value=_as_text(old),
                    new_value=_as_text(value),
                    actor=actor,
                    source_of_change="human",
                    edit_reason=reason,
                )
            )
            setattr(item, name, value)
            if name not in item.manual_overrides:
                item.manual_overrides.append(name)
        if not changed:
            return item
        with self.db.transaction():
            if "tags" in fields and fields["tags"] is not None:
                self.db.tags.set_tags(item_id, fields["tags"], is_manual=True)
                item.tags = self.db.tags.names(item_id)
            self.db.items.update(item)
            for revision in changed:
                self.db.items.add_revision(revision)
            self.db.search.rebuild(item_id)
        return item

    def set_visibility(
        self, item_id: int, scope: str = "feed", reason: str = "", *, restore: bool = False
    ) -> Item:
        """Скрыть или вернуть карточку. Удаления нет — только состояние видимости."""
        item = self.db.items.get(item_id)
        if item is None:
            raise ItemNotFoundError(f"карточка #{item_id} не найдена")
        target = {
            "feed": "hidden_feed",
            "digest": "hidden_digest",
            "deleted": "deleted",
            "visible": "visible",
        }.get("visible" if restore else scope)
        if target is None:
            raise ItemValidationError(f"неизвестная область скрытия: {scope}")
        if restore and item.visibility == "deleted":
            merged = self.db.items.last_revision(item_id, MERGED_FIELD)
            if merged is not None:
                # Публикации уже у другой карточки: вернуть эту — значит показать
                # в ленте пустую оболочку.
                raise ItemValidationError(
                    f"карточка #{item_id} объединена с #{merged.new_value}: "
                    "восстановление невозможно, её публикации теперь там"
                )
        self.db.items.set_visibility(item_id, target, "" if target == "visible" else reason)
        item.visibility, item.hidden_reason = target, reason
        return item

    def bulk_visibility(self, item_ids: list[int], scope: str = "digest", reason: str = "") -> int:
        """Подготовка дайджеста: одна атомарная операция, отменяется целиком."""
        target = {"feed": "hidden_feed", "digest": "hidden_digest", "visible": "visible"}.get(scope)
        if target is None:
            raise ItemValidationError(f"неизвестная область скрытия: {scope}")
        return self.db.items.set_visibility_bulk(item_ids, target, reason)

    def bulk_tags(
        self,
        item_ids: list[int],
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        actor: str = "user",
    ) -> dict:
        """Массовый тегинг: обычная работа аналитика, а не двадцать одиночных правок.

        Теги человека переживают переобработку (`is_manual`), поэтому здесь тот же
        признак, что и у одиночной правки. Индекс поиска перестраивается: тег
        входит в строку `items_search`.
        """
        added = [t.strip() for t in (add or []) if t and t.strip()]
        removed = [t.strip() for t in (remove or []) if t and t.strip()]
        if not added and not removed:
            raise ItemValidationError("нужен хотя бы один тег в add или remove")
        overlap = set(added) & set(removed)
        if overlap:
            raise ItemValidationError(f"тег нельзя добавить и снять сразу: {sorted(overlap)}")
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return {"changed": 0, "items": []}
        touched: list[int] = []
        with self.db.transaction():
            for item_id in ids:
                item = self.db.items.get(item_id)
                if item is None:
                    raise ItemNotFoundError(f"карточка #{item_id} не найдена")
                before = self.db.tags.names(item_id)
                self.db.tags.add(item_id, added, is_manual=True)
                self.db.tags.remove(item_id, removed)
                after = self.db.tags.names(item_id)
                if after == before:
                    continue
                item.tags = after
                self.db.items.update(item)
                self.db.items.add_revision(
                    ItemRevision(
                        item_id=item_id,
                        field="tags",
                        old_value=", ".join(before),
                        new_value=", ".join(after),
                        actor=actor,
                        source_of_change="human",
                        edit_reason="bulk_tags",
                    )
                )
                if "tags" not in item.manual_overrides:
                    item.manual_overrides.append("tags")
                    self.db.items.update(item)
                self.db.search.rebuild(item_id)
                touched.append(item_id)
        return {"changed": len(touched), "items": touched}

    def bulk_archive(self, item_ids: list[int], archived: bool = True, *, actor: str = "user") -> dict:
        """Убрать пачку карточек в архив (или вернуть). Ничего не удаляется."""
        ids = list(dict.fromkeys(item_ids))
        if not ids:
            return {"changed": 0, "items": []}
        touched: list[int] = []
        with self.db.transaction():
            for item_id in ids:
                item = self.db.items.get(item_id)
                if item is None:
                    raise ItemNotFoundError(f"карточка #{item_id} не найдена")
                if item.is_archived == archived:
                    continue
                self.db.items.set_archived(item_id, archived)
                self.db.items.add_revision(
                    ItemRevision(
                        item_id=item_id,
                        field="is_archived",
                        old_value=str(int(item.is_archived)),
                        new_value=str(int(archived)),
                        actor=actor,
                        source_of_change="human",
                        edit_reason="archive" if archived else "unarchive",
                    )
                )
                touched.append(item_id)
        return {"changed": len(touched), "items": touched}

    def add_note(self, item_id: int, body: str, author: str = "") -> ItemNote:
        """Заметка аналитика: решение сотрудника, а не факт из источника (US-7)."""
        if not body.strip():
            raise ItemValidationError("пустая заметка")
        if self.db.items.get(item_id) is None:
            raise ItemNotFoundError(f"карточка #{item_id} не найдена")
        note = ItemNote(item_id=item_id, body=body.strip(), author=author)
        with self.db.transaction():
            self.db.notes.add(note)
        return note

    def revert(self, item_id: int, field: str) -> Item:
        """Вернуть версию модели из истории (US-8)."""
        item = self.db.items.get(item_id)
        if item is None:
            raise ItemNotFoundError(f"карточка #{item_id} не найдена")
        if field not in REVERTIBLE_FIELDS:
            raise ItemValidationError(f"revert доступен для {list(REVERTIBLE_FIELDS)}"
            )
        revision = self.db.items.last_model_value(item_id, field)
        if revision is None or revision.new_value is None:
            raise NothingToRevertError(f"у карточки #{item_id} нет версии модели для поля «{field}»",
            )
        current = getattr(item, field)
        restored = (
            [t.strip() for t in revision.new_value.split(",") if t.strip()]
            if isinstance(current, list)
            else revision.new_value
        )
        setattr(item, field, restored)
        if field in item.manual_overrides:
            item.manual_overrides.remove(field)
        with self.db.transaction():
            self.db.items.update(item)
            if field == "tags":
                self.db.tags.set_tags(item_id, restored, is_manual=False)
            self.db.search.rebuild(item_id)
            self.db.items.add_revision(
                ItemRevision(
                    item_id=item_id,
                    field=field,
                    old_value=_as_text(current),
                    new_value=revision.new_value,
                    actor="user",
                    source_of_change="human",
                    edit_reason="revert",
                )
            )
        return item

    def add_npa_event(
        self,
        item_id: int,
        status: str,
        *,
        occurred_at: str | None = None,
        source_url: str = "",
        note: str = "",
        actor: str = "user",
    ) -> NpaEvent:
        """Событие в хронологии: статус НПА, слушания, срок. Статус из словаря двигает карточку."""
        item = self._require(item_id)
        status = (status or "").strip()
        if not status:
            raise ItemValidationError("пустой статус события")
        if len(status) > 64:
            raise ItemValidationError("статус события длиннее 64 символов")
        occurred_at = occurred_at or None
        if occurred_at:
            moment = parse_datetime(occurred_at)
            if moment is None:
                raise ItemValidationError(f"дата события не разбирается: {occurred_at!r}")
            occurred_at = to_utc_iso(moment)
        event = NpaEvent(
            item_id=item_id,
            status=status,
            occurred_at=occurred_at,
            source_url=source_url,
            note=note,
            created_by=actor,
            created_at=to_utc_iso(utc_now()) or "",
        )
        with self.db.transaction():
            event.id = self.db.items.add_event(event)
            # Новость не получает статус НПА от события; у НПА статус движется только вперёд
            # и не затирает правку человека.
            if (
                item.type == "npa"
                and advances_status(item.npa_status, status)
                and "npa_status" not in item.manual_overrides
            ):
                item.npa_status = status
                self.db.items.update(item)
        return event

    # -- архив --

    def set_archived(self, item_id: int, archived: bool, *, actor: str = "user") -> Item:
        """Архив — не удаление: карточка уходит из ленты и дайджеста, но остаётся в поиске.

        Для НПА снимается и уникальность `npa_key`: следующая публикация о том же
        акте заведёт новую карточку, а не присоединится к архивной.
        """
        item = self._require(item_id)
        if item.is_archived == archived:
            return item
        with self.db.transaction():
            self.db.items.set_archived(item_id, archived)
            self.db.items.add_revision(
                ItemRevision(
                    item_id=item_id,
                    field="is_archived",
                    old_value=str(int(item.is_archived)),
                    new_value=str(int(archived)),
                    actor=actor,
                    source_of_change="human",
                    edit_reason="archive" if archived else "unarchive",
                )
            )
        item.is_archived = archived
        return item

    # -- вероятные дубли (вторая дедупликация) --

    def duplicate_proposal(self, item_id: int) -> dict | None:
        """Открытое предложение «вероятный дубль — объединить?» для карточки.

        Открыто, пока последняя ревизия по `duplicate_of` — от модели. Партнёры,
        которых тем временем удалили, объединили или заархивировали, отпадают;
        без партнёров предложения нет.
        """
        revision = self.db.items.last_revision(item_id, DUPLICATE_FIELD)
        if revision is None or revision.source_of_change != "llm":
            return None
        payload = _proposal_payload(revision.new_value)
        partners = []
        for other_id in payload.get("items", []):
            other = self.db.items.get(int(other_id))
            if other is None or not _mergeable(other):
                continue
            partners.append(
                {"id": other.id, "title": other.title, "published_at": other.published_at}
            )
        if not partners:
            return None
        return {
            "items": partners,
            "similarity": payload.get("similarity"),
            "run_id": payload.get("run_id"),
            "created_at": revision.created_at,
        }

    def propose_duplicate(
        self, item_id: int, partners: list[int], *, similarity: float, run_id: int | None
    ) -> bool:
        """Записать предложение модели; повтор уже известного или отклонённого — нет.

        Вызывающий (прогон обработки) владеет транзакцией.
        """
        wanted = {int(p) for p in partners if int(p) != item_id}
        if not wanted:
            return False
        last = self.db.items.last_revision(item_id, DUPLICATE_FIELD)
        if last is not None:
            payload = _proposal_payload(last.new_value)
            if last.source_of_change == "llm" and wanted <= set(payload.get("items", [])):
                return False  # то же предложение уже висит на карточке
            if last.source_of_change == "human" and wanted <= set(
                payload.get("not_duplicate", [])
            ):
                return False  # аналитик уже сказал «не дубль» — не переспрашиваем
        self.db.items.add_revision(
            ItemRevision(
                item_id=item_id,
                field=DUPLICATE_FIELD,
                old_value=last.new_value if last else None,
                new_value=json.dumps(
                    {"items": sorted(wanted), "similarity": similarity, "run_id": run_id},
                    ensure_ascii=False,
                ),
                actor="model",
                source_of_change="llm",
                edit_reason="clustering",
            )
        )
        return True

    def dismiss_duplicate(self, item_id: int, *, actor: str = "user") -> dict:
        """«Не дубль»: закрыть предложение на карточке и у всех её партнёров.

        Решение записывается в историю как ревизия человека; следующий прогон
        не предложит ту же группу снова.
        """
        self._require(item_id)
        proposal = self.duplicate_proposal(item_id)
        if proposal is None:
            raise NoDuplicateProposalError(
                f"у карточки #{item_id} нет открытого предложения «вероятный дубль»"
            )
        partner_ids = [int(p["id"]) for p in proposal["items"]]
        group = [item_id, *partner_ids]
        with self.db.transaction():
            for member in group:
                others = sorted(i for i in group if i != member)
                last = self.db.items.last_revision(member, DUPLICATE_FIELD)
                self.db.items.add_revision(
                    ItemRevision(
                        item_id=member,
                        field=DUPLICATE_FIELD,
                        old_value=last.new_value if last else None,
                        new_value=json.dumps({"not_duplicate": others}),
                        actor=actor,
                        source_of_change="human",
                        edit_reason="not_duplicate",
                    )
                )
        return {"id": item_id, "dismissed": partner_ids}

    def merge(
        self, target_id: int, item_ids: list[int], *, actor: str = "user", reason: str = ""
    ) -> dict:
        """Объединить карточки-дубли: публикации, заметки и ручные теги переходят к `target`.

        Поглощённые карточки не удаляются физически — они `deleted` с причиной
        «объединена с #N» и уходят из ленты и дайджеста. НПА не объединяются:
        тождество акта — только `npa_key`. Приоритет принимающей карточки —
        максимум по группе: потерять `high` дороже лишней строки в ленте.
        """
        target = self._require(target_id)
        others = [
            self._require(other_id)
            for other_id in dict.fromkeys(int(i) for i in item_ids)
            if other_id != target_id
        ]
        if not others:
            raise ItemValidationError("нужна хотя бы одна другая карточка для объединения")
        for card in (target, *others):
            if card.type == "npa":
                raise ItemValidationError(
                    f"карточка #{card.id} — НПА: тождество акта только по npa_key, "
                    "объединение по похожести запрещено"
                )
            if card.visibility == "deleted":
                raise ItemValidationError(f"карточка #{card.id} удалена или уже объединена")
        absorbed: list[int] = []
        with self.db.transaction():
            for other in others:
                moved = self.db.items.move_sources(other.id, target.id)
                cluster = self.db.clusters.get(other.cluster_id)
                self.db.clusters.grow(
                    target.cluster_id,
                    moved,
                    divergent=bool(cluster and cluster.has_divergent_opinions),
                )
                manual = self.db.tags.manual_names(other.id)
                if manual:
                    self.db.tags.add(target.id, manual, is_manual=True)
                self.db.notes.reassign(other.id, target.id)
                self.db.items.mark_merged(other.id, target.id)
                self.db.items.add_revision(
                    ItemRevision(
                        item_id=other.id,
                        field=MERGED_FIELD,
                        old_value=None,
                        new_value=str(target.id),
                        actor=actor,
                        source_of_change="human",
                        edit_reason=reason or "merge",
                    )
                )
                self.db.search.remove(other.id)
                absorbed.append(other.id)
            top = min((c.priority for c in (target, *others)), key=_priority_rank)
            if top != target.priority:
                self.db.items.add_revision(
                    ItemRevision(
                        item_id=target.id,
                        field="priority",
                        old_value=target.priority,
                        new_value=top,
                        actor=actor,
                        source_of_change="human",
                        edit_reason=reason or "merge",
                    )
                )
                target.priority = top
                if "priority" not in target.manual_overrides:
                    target.manual_overrides.append("priority")
            target.tags = self.db.tags.names(target.id)
            self.db.items.update(target)
            last = self.db.items.last_revision(target.id, DUPLICATE_FIELD)
            self.db.items.add_revision(
                ItemRevision(
                    item_id=target.id,
                    field=DUPLICATE_FIELD,
                    old_value=last.new_value if last else None,
                    new_value=json.dumps({"merged": absorbed}),
                    actor=actor,
                    source_of_change="human",
                    edit_reason=reason or "merge",
                )
            )
            self.db.search.rebuild(target.id)
        return {
            "item": target,
            "absorbed": absorbed,
            "sources_count": self.db.items.source_count(target.id),
        }

    # -- history --

    def record_model_revisions(self, item_id: int, item: Item, previous: Item | None = None) -> None:
        """Write what the model produced into the history.

        Without this `revert` has nothing to return: the card would only ever hold
        the human's value and the model's version would be lost (US-8).
        """
        for field_name in REVERTIBLE_FIELDS:
            value = _as_text(getattr(item, field_name))
            old = _as_text(getattr(previous, field_name)) if previous is not None else None
            if previous is not None and old == value:
                continue
            self.db.items.add_revision(
                ItemRevision(
                    item_id=item_id,
                    field=field_name,
                    old_value=old,
                    new_value=value,
                    actor="model",
                    source_of_change="llm",
                )
            )


def advances_status(current: str | None, candidate: str) -> bool:
    """True when `candidate` is a later stage than `current` — statuses never go back."""
    if candidate not in _STATUS_ORDER:
        return False
    if not current or current not in _STATUS_ORDER:
        return True
    return _STATUS_ORDER.index(candidate) > _STATUS_ORDER.index(current)


def _as_text(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(map(str, value))
    return "" if value is None else str(value)


def _proposal_payload(raw: str | None) -> dict:
    """JSON ревизии `duplicate_of`; битое или пустое значение — пустой словарь."""
    try:
        payload = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _mergeable(item: Item) -> bool:
    """С кем ещё можно объединиться: живая новость, не архив, не НПА."""
    return item.type == "news" and item.visibility != "deleted" and not item.is_archived


def _priority_rank(priority: str) -> int:
    return PRIORITIES.index(priority) if priority in PRIORITIES else len(PRIORITIES)
