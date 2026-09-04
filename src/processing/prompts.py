"""Prompt templates for S2-S5 and the extractive fallback.

Only public document text and the company profile reach this module. The
analyst's own note is deliberately not an argument anywhere here — the data
perimeter is enforced by the shape of the API, not by remembering to strip a
field (spec: «`analyst_note` никогда не отправляются во внешние LLM API»).
"""

from __future__ import annotations

import json

from ..models import ITEM_TAGS
from .schema import RESULT_SCHEMA

STAGE = "s2_s5"

SYSTEM = """Ты — аналитик GR-мониторинга российской ИТ-компании.
Отвечай только по присланному тексту, ничего не додумывая.

Правила:
1. summary — от 3 до 5 предложений на русском языке. Без вводных конструкций
   («В данной статье говорится», «Автор отмечает»). Сразу суть: кто, что, когда,
   какие последствия.
2. entities.who / what / when / impact — короткие формулировки из текста.
   Если поле из текста не выводится, поставь null. Догадка хуже пустого поля.
3. evidence_offsets — по одной паре [начало, конец] на каждое предложение summary,
   в том же порядке. Это координаты символов присланного текста: срез
   текст[начало:конец] обязан содержать факты этого предложения. Пары обязательны.
4. type: npa — нормативный правовой акт, законопроект, приказ, постановление,
   регуляторная инициатива; news — всё остальное.
5. npa_status заполняй только для type=npa и только если статус явно следует из текста.
   Допустимые значения — ровно эти: анонс, разработка, внесён, рассмотрение, принят,
   действует. Своих формулировок («разрабатывается», «на рассмотрении») не придумывай:
   их отбросит валидация, и карточка останется без статуса.
6. npa_key — идентификатор акта строго из текста: номер законопроекта СОЗД,
   идентификатор проекта на regulation.gov.ru, номер и дата принятого акта.
   Нет явного идентификатора — null. Не сочиняй и не выводи его из заголовка.
7. priority — относительно профиля компании ниже. high: прямо затрагивает продукты,
   лицензии, налоговый режим, регуляторов или рынок компании. medium: отрасль в целом.
   low: тематически близко, но к компании не относится (смотри «НЕ относится к компании»).
8. reasoning — 1-2 предложения, почему именно такой приоритет. Их читает человек.
9. relevance_score и confidence — числа от 0 до 1. confidence занижай, если текст
   обрезан пейволлом или это только анонс.
10. tags — только из списка: {tags}. Ничего не выдумывай, пустой список допустим.

Ответ — один JSON-объект и ничего больше: без рассуждений, без пояснений до или после,
без markdown-разметки. Объект обязан соответствовать JSON Schema ниже; перечисления
(enum) и границы массивов соблюдай буквально.

{schema}"""

TEMPLATE = """{profile}

---
Документ.
Источник: {source}
Заголовок: {title}
Дата публикации: {published}

Текст (evidence_offsets считаются по этому тексту, нумерация символов с нуля):
{text}"""


def system_prompt() -> str:
    """The system message, with the response schema spelled out inside it.

    Ollama Cloud accepts `format` and ignores it (docs.ollama.com: «Ollama's Cloud
    currently does not support structured outputs»), so the schema has to reach the
    model as text or nothing constrains the answer at all. Locally the grammar and
    this block say the same thing, which is what the vendor recommends anyway.
    """
    return SYSTEM.format(
        tags=", ".join(ITEM_TAGS),
        schema=json.dumps(RESULT_SCHEMA, ensure_ascii=False, indent=1),
    )


def build(
    *,
    text: str,
    title: str = "",
    source: str = "",
    published: str = "",
    profile_block: str = "",
) -> str:
    """One cluster's prompt. Takes document fields only — never a card, never a note."""
    return TEMPLATE.format(
        profile=profile_block or "Профиль компании не задан.",
        source=source or "не указан",
        title=title or "без заголовка",
        published=published or "не указана",
        text=text,
    )
