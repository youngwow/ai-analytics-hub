#!/usr/bin/env python3
"""Build the hand-authored B3 E2E scenario dataset.

No model is called here. Every publication, label and expected object below is
written explicitly so generation and evaluation remain independent of the
system under test.
"""

from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "v1"


def dump_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def dump_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows),
        encoding="utf-8",
    )


items: list[dict] = []
truth: list[dict] = []


def add(
    item_id: str,
    flow: str,
    minute: int,
    surface: str,
    discoverability: list[str],
    title: str,
    text: str,
    *,
    relevance: str,
    importance: str = "low",
    roles: list[str] | None = None,
    topic: str = "noise",
    object_id: str | None = None,
    object_type: str = "publication",
    critical: bool = False,
    urgent: bool = False,
    include_in_digest: bool = False,
    risk_reasons: list[str] | None = None,
    must_facts: list[str] | None = None,
    forbidden_claims: list[str] | None = None,
) -> None:
    day = "2026-08-24" if flow == "A" else "2026-08-25" if flow == "B" else "2026-08-26"
    hour = 8 + minute // 60
    mins = minute % 60
    source_names = {
        "rss": "Деловая лента",
        "telegram": "Отраслевой канал",
        "media_html": "Технологический вестник",
        "regulator_html": "Официальный регулятор",
        "corporate_html": "Корпоративный пресс-центр",
        "search_api": "Дополнительный веб-поиск",
    }
    items.append(
        {
            "id": item_id,
            "flow_id": flow,
            "available_at": f"{day}T{hour:02d}:{mins:02d}:00+03:00",
            "surface": surface,
            "source_name": source_names[surface],
            "source_url": f"https://{surface}.example.invalid/{item_id.lower()}",
            "discoverability": discoverability,
            "title": title,
            "raw_text": text,
            "language": "ru",
        }
    )
    truth.append(
        {
            "id": item_id,
            "relevance": relevance,
            "importance": importance,
            "roles": roles or [],
            "topic": topic,
            "object_id": object_id,
            "object_type": object_type,
            "critical": critical,
            "urgent": urgent,
            "include_in_digest": include_in_digest,
            "risk_reasons": risk_reasons or [],
            "must_facts": must_facts or [],
            "forbidden_claims": forbidden_claims or [],
        }
    )


# Flow A: typical working day. Six plausible distractions.
add("A01", "A", 5, "rss", ["fixed"], "Ритейлер тестирует электронные ценники",
    "Сеть магазинов одежды начала пилот электронных ценников в десяти торговых точках. Цены обновляются из центральной учётной системы, а сотрудники управляют экранами через мобильный терминал.", relevance="irrelevant")
add("A02", "A", 18, "telegram", ["fixed"], "Нейросеть выбирает музыку для кафе",
    "Сервис фоновой музыки добавил генеративные плейлисты для ресторанов. Решение предназначено только для заведений общественного питания.", relevance="irrelevant", risk_reasons=["generic_ai_keyword_noise"])
add("A03", "A", 33, "media_html", ["fixed"], "Завод упаковки расширил линию",
    "Производитель картонной упаковки запустил новую линию в Тверской области. На короба наносятся цифровые коды маркировки, а камеры автоматически проверяют качество печати.", relevance="irrelevant", risk_reasons=["keyword_false_positive"])
add("A04", "A", 55, "rss", ["fixed"], "Онлайн-кинотеатр обновил каталог сериалов",
    "Платформа приобрела права на семь зарубежных драм и опубликовала календарь премьер на осень. Первые два сериала появятся в каталоге 4 сентября.", relevance="irrelevant")
add("A05", "A", 77, "telegram", ["fixed"], "Банк открыл школу аналитиков",
    "Банк начал бесплатный курс анализа данных для студентов. В программе — SQL, визуализация отчётов и итоговый проект на обезличенных банковских данных.", relevance="irrelevant")
add("A06", "A", 102, "rss", ["fixed"], "Рынок умных кормушек вырос за квартал",
    "Обзор интернет-магазина сообщает о росте продаж умных кормушек для домашних животных на 18%. Покупатели чаще выбирают модели с бытовой камерой и мобильным приложением.", relevance="irrelevant")

# Four standalone relevant publications.
add("A07", "A", 24, "regulator_html", ["fixed"], "Проект требований к защищённой загрузке абонентских устройств",
    "Регулятор вынес на обсуждение требования к проверке цифровой подписи системного ПО абонентских устройств. Замечания принимаются до 18 сентября 2026 года. Требования предлагается применять к новым моделям через девять месяцев после утверждения.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="device_security", object_id="OBJ-A07", include_in_digest=True,
    risk_reasons=["deadline", "company_impact_requires_interpretation"],
    must_facts=["Замечания принимаются до 18 сентября 2026 года.", "Применение предложено через девять месяцев после утверждения."],
    forbidden_claims=["Требования уже вступили в силу.", "GS Labs обязана заменить выпущенные устройства."])
add("A08", "A", 68, "corporate_html", ["fixed"], "GS Labs открыла набор в программу для разработчиков приложений",
    "GS Labs объявила набор команд в программу разработки приложений для телевизионных платформ. Участникам предоставят документацию и тестовые устройства; коммерческий запуск их продуктов не гарантируется.",
    relevance="relevant", importance="medium", roles=["PR", "HEAD"], topic="company", object_id="OBJ-A08", include_in_digest=True,
    must_facts=["Открыт набор команд в программу разработки приложений.", "Коммерческий запуск продуктов не гарантируется."],
    forbidden_claims=["GS Labs инвестирует в каждую команду."])
add("A09", "A", 126, "rss", ["fixed"], "Региональный оператор объявил конкурс на телевизионную платформу",
    "Оператор связи «Северный поток» объявил конкурс на middleware и клиентское ПО для 180 тысяч приставок. Приём заявок завершается 12 сентября; требования допускают российские решения и не называют победителя.",
    relevance="relevant", importance="high", roles=["PR", "HEAD"], topic="market_opportunity", object_id="OBJ-A09", include_in_digest=True,
    risk_reasons=["opportunity_not_contract"], must_facts=["Конкурс рассчитан на ПО для 180 тысяч приставок.", "Заявки принимаются до 12 сентября."],
    forbidden_claims=["GS Labs выиграла конкурс.", "Оператор выбрал платформу GS Labs."])
add("A10", "A", 171, "media_html", ["fixed"], "Эксперты обсудили прозрачность рекомендательных систем",
    "На отраслевой конференции предложили добровольно раскрывать пользователям основные принципы рекомендаций видеоконтента. Это позиция участников дискуссии, а не опубликованный нормативный акт.",
    relevance="borderline", importance="medium", roles=["GR", "PR"], topic="recommendation_systems", object_id="OBJ-A10", include_in_digest=False,
    risk_reasons=["opinion_not_regulation"], must_facts=["Предложение прозвучало на конференции и носит добровольный характер."],
    forbidden_claims=["Регулятор обязал платформы раскрыть алгоритмы."])

# Multi-source event A-E1: one fact, complementary details and an independent position.
add("A11", "A", 210, "regulator_html", ["fixed"], "Опубликован стандарт доступности интерфейсов Smart TV",
    "Орган стандартизации утвердил добровольный стандарт доступности интерфейсов Smart TV. Он содержит рекомендации по контрастности, озвучиванию меню и управлению без точных жестов. Стандарт действует с 1 января 2027 года.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="accessibility", object_id="EV-A1", object_type="event", include_in_digest=True,
    must_facts=["Стандарт является добровольным.", "Он действует с 1 января 2027 года."], forbidden_claims=["Все производители обязаны выполнить стандарт."])
add("A12", "A", 224, "rss", ["fixed"], "Новый стандарт затронет интерфейсы телевизоров",
    "Утверждён стандарт доступности Smart TV: рекомендации касаются контраста и голосового сопровождения. Издание ошибочно называет применение обязательным, хотя официальный текст определяет стандарт как добровольный.",
    relevance="relevant", importance="high", roles=["GR", "PR", "HEAD"], topic="accessibility", object_id="EV-A1", object_type="event", include_in_digest=True,
    risk_reasons=["secondary_source_conflicts_with_primary"], must_facts=["Издание называет применение обязательным."], forbidden_claims=[])
add("A13", "A", 239, "telegram", ["fixed"], "Ассоциация просит дать рынку больше времени",
    "Ассоциация разработчиков телевизионных платформ поддержала цели стандарта доступности, но считает срок подготовки экосистемы недостаточным. Это самостоятельная позиция ассоциации; новых юридических требований сообщение не вводит.",
    relevance="relevant", importance="medium", roles=["GR", "PR"], topic="accessibility", object_id="EV-A1", object_type="event", include_in_digest=True,
    risk_reasons=["independent_opinion"], must_facts=["Ассоциация поддержала цели, но считает срок подготовки недостаточным."], forbidden_claims=["Срок действия стандарта перенесён."])

# Multi-source event A-E2: corporate pilot with different numbers and opinion.
add("A14", "A", 285, "corporate_html", ["fixed"], "GS Group запустила пилот локального телевидения в трёх районах",
    "GS Group сообщила о запуске пилота локального телевидения в трёх районах Калининградской области. На первом этапе подключены 12 тысяч домохозяйств; технологическую платформу предоставляет GS Labs.",
    relevance="relevant", importance="high", roles=["PR", "HEAD"], topic="company", object_id="EV-A2", object_type="event", include_in_digest=True,
    must_facts=["Пилот запущен в трёх районах.", "На первом этапе подключены 12 тысяч домохозяйств.", "Платформу предоставляет GS Labs."], forbidden_claims=[])
add("A15", "A", 302, "rss", ["fixed"], "Локальное телевидение GS Group охватит до 20 тысяч семей",
    "Региональное издание пишет, что пилот уже доступен 12 тысячам домохозяйств, а потенциальный охват после расширения может составить 20 тысяч. Расширение пока не подтверждено как завершённое.",
    relevance="relevant", importance="high", roles=["PR", "HEAD"], topic="company", object_id="EV-A2", object_type="event", include_in_digest=True,
    risk_reasons=["actual_vs_forecast_number"], must_facts=["Текущий охват — 12 тысяч домохозяйств.", "20 тысяч — потенциальный охват после расширения."], forbidden_claims=["Пилот уже охватывает 20 тысяч семей."])
add("A16", "A", 330, "telegram", ["fixed"], "Эксперт: локальные каналы потребуют устойчивой модели контента",
    "Медиаэксперт назвал технологическую часть пилота убедительной, но предупредил, что масштабирование зависит от экономики локального контента. Это оценка эксперта, а не результат пилота.",
    relevance="relevant", importance="medium", roles=["PR", "HEAD"], topic="company", object_id="EV-A2", object_type="event", include_in_digest=True,
    risk_reasons=["independent_opinion"], must_facts=["Эксперт связал масштабирование с экономикой локального контента."], forbidden_claims=["Пилот доказал нерентабельность локального телевидения."])

# NPA A trajectory.
add("A17", "A", 61, "regulator_html", ["fixed"], "Обсуждение проекта 26-041 о предустановке медиаприложений",
    "На портале проектов опубликован проект 26-041. Он предлагает включить отечественное медиаприложение в перечень программ для предустановки на новые телевизионные устройства. Обсуждение открыто до 2 сентября 2026 года.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="preinstallation", object_id="NPA-A", object_type="npa", include_in_digest=True,
    risk_reasons=["new_npa"], must_facts=["Проект имеет номер 26-041.", "Обсуждение открыто до 2 сентября 2026 года."], forbidden_claims=["Требование уже обязательно."])
add("A18", "A", 421, "regulator_html", ["fixed"], "Проект 26-041 внесён в парламент с изменённой датой применения",
    "Проект 26-041 внесён в парламент. В новой редакции применение предлагается с 1 сентября 2027 года вместо 1 марта 2027 года. Требование по-прежнему касается новых телевизионных устройств.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="preinstallation", object_id="NPA-A", object_type="npa", include_in_digest=True,
    risk_reasons=["stage_and_version_change"], must_facts=["Проект внесён в парламент.", "Дата применения изменена на 1 сентября 2027 года."], forbidden_claims=["Закон принят."])
add("A19", "A", 455, "telegram", ["fixed"], "Предустановка медиаприложений станет обязательной весной",
    "Канал повторяет раннюю версию проекта 26-041 и утверждает, что правила начнут действовать 1 марта 2027 года. Официальная внесённая редакция уже содержит дату 1 сентября 2027 года.",
    relevance="relevant", importance="medium", roles=["GR"], topic="preinstallation", object_id="NPA-A", object_type="npa", include_in_digest=False,
    risk_reasons=["stale_repost"], must_facts=["Канал повторяет устаревшую дату 1 марта 2027 года."], forbidden_claims=["Актуальная дата применения — 1 марта 2027 года."])

# Search-only A: one useful find and two distractions.
add("A20", "A", 146, "search_api", ["search"], "Техзадание муниципальной сети требует совместимости с двумя CAS",
    "В приложении к закупке муниципального оператора указана обязательная одновременная совместимость клиентского ПО с CAS «Ключ-А» и «Ключ-Б». Плановый объём — 45 тысяч устройств; заявки принимаются до 9 сентября. Документ не упоминался в постоянных источниках.",
    relevance="relevant", importance="high", roles=["PR", "HEAD"], topic="market_opportunity", object_id="OBJ-A20", include_in_digest=True,
    risk_reasons=["search_only_primary_document"], must_facts=["Требуется совместимость с двумя CAS.", "Плановый объём — 45 тысяч устройств.", "Заявки принимаются до 9 сентября."], forbidden_claims=["GS Labs соответствует требованиям закупки.", "GS Labs выиграла закупку."])
add("A21", "A", 190, "search_api", ["search"], "CAS для автоматизации расчёта зарплат",
    "Университетская лаборатория показала, как Computer Algebra System сокращает время проверки формул расчёта зарплат. Авторы опубликовали учебный блокнот и набор примеров.", relevance="irrelevant", risk_reasons=["acronym_collision"])
add("A22", "A", 370, "search_api", ["search"], "Обзор приставок 2022 года снова появился в выдаче",
    "Архивный обзор от ноября 2022 года сравнивает четыре модели Android-приставок по памяти, набору разъёмов и цене на момент публикации. Две модели позже были сняты с производства.", relevance="irrelevant", risk_reasons=["stale_search_result"])

# Flow B: independent but structurally balanced working day.
add("B01", "B", 8, "rss", ["fixed"], "Сервис доставки внедрил голосовое меню", "Сервис доставки еды добавил в мобильное приложение голосовой поиск ресторанов. Компания сообщает, что участники пилота стали оформлять повторные заказы на 11% быстрее.", relevance="irrelevant")
add("B02", "B", 21, "telegram", ["fixed"], "Фестиваль цифрового искусства объявил программу", "Организаторы фестиваля опубликовали список интерактивных инсталляций и лекций о генеративной графике. В главном зале установят двенадцать широкоформатных экранов.", relevance="irrelevant", risk_reasons=["generic_digital_keyword"])
add("B03", "B", 44, "media_html", ["fixed"], "Производитель мебели открыл шоурум", "Компания открыла мебельный шоурум с сенсорными экранами для каталога и цифровой навигацией по залу. Проект охватывает два этажа и более тысячи товарных позиций.", relevance="irrelevant")
add("B04", "B", 70, "rss", ["fixed"], "Стриминговый блог назвал лучшие комедии", "Редакционная подборка рекомендует десять комедий на выходные. Читатели могут проголосовать за любимый фильм, а итоговый рейтинг обновится в понедельник.", relevance="irrelevant")
add("B05", "B", 95, "telegram", ["fixed"], "Курс по Python обновил учебную программу", "Онлайн-школа добавила модуль по анализу изображений. Студенты соберут классификатор фотографий и представят итоговый проект на открытом вебинаре.", relevance="irrelevant")
add("B06", "B", 116, "rss", ["fixed"], "Дизайнер представил умную лампу", "Краудфандинговый проект предлагает декоративную лампу с пультом и мобильным приложением. Прототип поддерживает сценарии освещения и синхронизацию с музыкой.", relevance="irrelevant")

add("B07", "B", 29, "regulator_html", ["fixed"], "ЦОД предложили отчитываться об энергоэффективности",
    "Проект методики вводит ежегодную отчётность об энергоэффективности центров обработки данных мощностью свыше 500 кВт. Публичное обсуждение продлится до 22 сентября; обязательность пока не установлена.",
    relevance="borderline", importance="medium", roles=["GR", "HEAD"], topic="data_centers", object_id="OBJ-B07", include_in_digest=False,
    risk_reasons=["scope_to_be_confirmed"], must_facts=["Порог проекта — свыше 500 кВт.", "Обсуждение идёт до 22 сентября."], forbidden_claims=["GS Labs обязана отчитываться."])
add("B08", "B", 83, "corporate_html", ["fixed"], "GS Labs получила награду за платформу интерактивного телевидения",
    "Проект GS Labs получил отраслевую награду за пользовательский интерфейс интерактивного телевидения. Награда не содержит данных о продажах или новых контрактах.",
    relevance="relevant", importance="medium", roles=["PR", "HEAD"], topic="company", object_id="OBJ-B08", include_in_digest=True,
    must_facts=["Награда присуждена за пользовательский интерфейс интерактивного телевидения."], forbidden_claims=["Награда подтверждает лидерство по доле рынка."])
add("B09", "B", 132, "rss", ["fixed"], "Регион расширил субсидии производителям радиоэлектроники",
    "Программа компенсирует до 20% затрат на пилотные партии радиоэлектронной продукции, но не более 30 млн рублей на проект. Заявки принимаются до 30 сентября.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="state_support", object_id="OBJ-B09", include_in_digest=True,
    risk_reasons=["eligibility_not_confirmed"], must_facts=["Компенсация — до 20% затрат.", "Лимит — 30 млн рублей на проект.", "Заявки принимаются до 30 сентября."], forbidden_claims=["GS Labs гарантированно получит субсидию."])
add("B10", "B", 178, "telegram", ["fixed"], "Исследователи раскрыли уязвимость медиаплеера OpenView",
    "Исследовательская группа описала удалённое выполнение кода в библиотеке OpenView 4.1. Неизвестно, используется ли эта библиотека в продуктах GS Labs; производитель выпустил исправление 4.1.2.",
    relevance="unknown", importance="high", roles=["HEAD"], topic="security", object_id="OBJ-B10", include_in_digest=True,
    risk_reasons=["internal_dependency_unknown", "security"], must_facts=["Уязвима библиотека OpenView 4.1.", "Выпущено исправление 4.1.2.", "Использование библиотеке в GS Labs неизвестно."], forbidden_claims=["Продукты GS Labs уязвимы."])

# Multi-source event B-E1: watermarking proposal.
add("B11", "B", 205, "regulator_html", ["fixed"], "Опубликована концепция маркировки легального видеоконтента",
    "Рабочая группа опубликовала концепцию цифровой маркировки легального видеоконтента. Документ предлагает пилот, а не обязательное внедрение; параметры пилота должны определить отдельно.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="content_protection", object_id="EV-B1", object_type="event", include_in_digest=True,
    must_facts=["Опубликована концепция пилота.", "Обязательное внедрение не установлено."], forbidden_claims=["Маркировка уже обязательна."])
add("B12", "B", 219, "rss", ["fixed"], "Онлайн-кинотеатры готовят обязательную маркировку видео",
    "Издание сообщает о будущей обязательной маркировке, хотя опубликованная концепция описывает только пилот. Сроки и участники пилота ещё не утверждены.",
    relevance="relevant", importance="high", roles=["GR", "PR"], topic="content_protection", object_id="EV-B1", object_type="event", include_in_digest=True,
    risk_reasons=["headline_overstates_status"], must_facts=["Сроки и участники пилота не утверждены."], forbidden_claims=["Обязательная маркировка утверждена."])
add("B13", "B", 244, "telegram", ["fixed"], "Правообладатели поддержали тест маркировки",
    "Объединение правообладателей поддержало пилот маркировки, но попросило не раскрывать публично технические параметры защиты. Это позиция объединения, не норма концепции.",
    relevance="relevant", importance="medium", roles=["GR", "PR"], topic="content_protection", object_id="EV-B1", object_type="event", include_in_digest=True,
    risk_reasons=["independent_opinion"], must_facts=["Правообладатели поддержали пилот и попросили ограничить раскрытие параметров защиты."], forbidden_claims=["Концепция запрещает раскрывать технические параметры."])

# Multi-source event B-E2: partnership, with a rumour that must remain attributed.
add("B14", "B", 276, "corporate_html", ["fixed"], "GS Labs и оператор «Орбита» начали технологический пилот",
    "GS Labs и оператор «Орбита» начали трёхмесячный пилот облачного интерфейса для 8 тысяч пользователей. Решение о промышленном внедрении будет принято после испытаний.",
    relevance="relevant", importance="high", roles=["PR", "HEAD"], topic="company", object_id="EV-B2", object_type="event", include_in_digest=True,
    must_facts=["Пилот рассчитан на три месяца и 8 тысяч пользователей.", "Решение о внедрении будет принято после испытаний."], forbidden_claims=["Подписан промышленный контракт."])
add("B15", "B", 294, "rss", ["fixed"], "«Орбита» тестирует облачный интерфейс на 8 тысячах пользователей",
    "Оператор подтвердил масштаб и длительность пилота с GS Labs. Представитель оператора отметил, что критериями будут стабильность и доля завершённых пользовательских сценариев.",
    relevance="relevant", importance="high", roles=["PR", "HEAD"], topic="company", object_id="EV-B2", object_type="event", include_in_digest=True,
    must_facts=["Критерии оператора — стабильность и завершение пользовательских сценариев."], forbidden_claims=[])
add("B16", "B", 317, "telegram", ["fixed"], "Источник: «Орбита» уже выбрала GS Labs для всей сети",
    "Анонимный канал утверждает, что промышленное решение якобы уже принято. Официальные стороны говорят только о трёхмесячном пилоте и последующей оценке.",
    relevance="relevant", importance="medium", roles=["PR", "HEAD"], topic="company", object_id="EV-B2", object_type="event", include_in_digest=True,
    risk_reasons=["unverified_rumour"], must_facts=["Анонимный канал заявляет о выборе для всей сети."], forbidden_claims=["«Орбита» выбрала GS Labs для всей сети."])

# NPA B trajectory without relying only on one stable URL.
add("B17", "B", 58, "regulator_html", ["fixed"], "Концепция правил удалённого обновления подключённых устройств",
    "Опубликована концепция требований к журналированию и откату удалённых обновлений подключённых устройств. Идентификатор обсуждения — RU-IOT-77; предложения принимаются до 5 сентября.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="device_updates", object_id="NPA-B", object_type="npa", include_in_digest=True,
    risk_reasons=["new_npa"], must_facts=["Идентификатор обсуждения — RU-IOT-77.", "Предложения принимаются до 5 сентября."], forbidden_claims=["Правила утверждены."])
add("B18", "B", 403, "regulator_html", ["fixed"], "Доработанный проект правил безопасного обновления устройств",
    "Регулятор опубликовал доработанный текст правил журналирования и отката обновлений. Страница получила новый URL и не повторяет идентификатор RU-IOT-77, но приложение ссылается на итоги того же обсуждения. Добавлен срок хранения журнала — 12 месяцев.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="device_updates", object_id="NPA-B", object_type="npa", include_in_digest=True,
    risk_reasons=["same_npa_without_visible_id", "version_change"], must_facts=["Новый текст связан с прежним обсуждением через приложение.", "Добавлен срок хранения журнала 12 месяцев."], forbidden_claims=["Это независимый новый проект."])
add("B19", "B", 438, "rss", ["fixed"], "Правила обновления устройств уже требуют хранить журналы год",
    "Новость описывает доработанный проект как действующее требование. В официальном источнике документ всё ещё находится на стадии проекта.",
    relevance="relevant", importance="medium", roles=["GR"], topic="device_updates", object_id="NPA-B", object_type="npa", include_in_digest=False,
    risk_reasons=["stage_overstatement"], must_facts=["Официальный документ остаётся проектом."], forbidden_claims=["Требование уже действует."])

add("B20", "B", 151, "search_api", ["search"], "Протокол форума операторов: спрос на единый API телеметрии",
    "В опубликованном протоколе технической секции четыре региональных оператора назвали отсутствие единого API телеметрии препятствием для обновления парка приставок. Решений о закупке нет, но зафиксирован общий рыночный запрос.",
    relevance="borderline", importance="medium", roles=["PR", "HEAD"], topic="market_signal", object_id="OBJ-B20", include_in_digest=False,
    risk_reasons=["search_only_market_signal"], must_facts=["Четыре оператора назвали отсутствие единого API препятствием.", "Решений о закупке нет."], forbidden_claims=["Операторы заказали API у GS Labs."])
add("B21", "B", 188, "search_api", ["search"], "API телеметрии для теплиц", "Разработчик агроплатформы открыл API телеметрии датчиков влажности и температуры. Документация описывает пакетную выгрузку показаний и тревоги для агронома.", relevance="irrelevant", risk_reasons=["keyword_false_positive"])
add("B22", "B", 362, "search_api", ["search"], "Архивная вакансия разработчика приставок", "В архивной вакансии от апреля 2021 года требовался разработчик Linux для телевизионных приставок. Приём откликов завершён, указанная команда была сформирована в том же году.", relevance="irrelevant", risk_reasons=["stale_search_result"])

# Rare episode D1: critical official correction and misleading secondary report.
add("D101", "D", 12, "regulator_html", ["fixed"], "Предписание об отзыве сертификатов библиотеки SecureBoot-X 2.4",
    "Регулятор приостановил действие сертификатов SecureBoot-X 2.4 из-за ошибки проверки подписи. Производителям устройств предписано в течение 30 дней подтвердить отсутствие версии 2.4 либо представить план обновления. Предписание действует с момента публикации.",
    relevance="relevant", importance="critical", roles=["GR", "HEAD"], topic="device_security", object_id="EV-D1", object_type="event", critical=True, urgent=True, include_in_digest=True,
    risk_reasons=["critical", "internal_dependency_unknown"], must_facts=["Сертификаты SecureBoot-X 2.4 приостановлены.", "Ответ требуется в течение 30 дней."], forbidden_claims=["GS Labs использует SecureBoot-X 2.4."])
add("D102", "D", 24, "rss", ["fixed"], "Все телевизионные приставки обязали срочно отозвать",
    "Издание заявило о всеобщем отзыве приставок, хотя официальное предписание касается сертификатов конкретной библиотеки и требует проверки состава ПО либо плана обновления.",
    relevance="relevant", importance="high", roles=["GR", "PR", "HEAD"], topic="device_security", object_id="EV-D1", object_type="event", include_in_digest=True,
    risk_reasons=["critical_misreporting"], must_facts=["Публикация ошибочно расширяет предмет предписания до всех приставок."], forbidden_claims=["Регулятор распорядился отозвать все приставки."])
add("D103", "D", 41, "regulator_html", ["fixed"], "Уточнение к предписанию по SecureBoot-X 2.4",
    "Регулятор уточнил, что устройства без SecureBoot-X 2.4 отзыву и дополнительной сертификации не подлежат. Срок ответа для производителей, использующих версию 2.4, остаётся 30 дней.",
    relevance="relevant", importance="critical", roles=["GR", "HEAD"], topic="device_security", object_id="EV-D1", object_type="event", critical=True, urgent=True, include_in_digest=True,
    risk_reasons=["critical_correction"], must_facts=["Устройства без версии 2.4 отзыву не подлежат.", "Срок ответа для затронутых производителей остаётся 30 дней."], forbidden_claims=["Предписание полностью отменено."])

# Rare episode D2: similar vocabulary but two different events.
add("D201", "D", 90, "rss", ["fixed"], "Оператор «Маяк» начал пилот голосового управления",
    "Оператор «Маяк» начал двухмесячный пилот голосового управления приставкой для 3 тысяч пользователей в Перми.",
    relevance="relevant", importance="medium", roles=["PR", "HEAD"], topic="voice_ui", object_id="EV-D2A", object_type="event", include_in_digest=True,
    must_facts=["Пилот «Маяка» идёт в Перми для 3 тысяч пользователей."], forbidden_claims=[])
add("D202", "D", 97, "telegram", ["fixed"], "«Маяк» тестирует голосовой интерфейс приставки",
    "Канал пересказывает сообщение оператора о двухмесячном пилоте для 3 тысяч пользователей в Перми без новых фактов.",
    relevance="relevant", importance="medium", roles=["PR"], topic="voice_ui", object_id="EV-D2A", object_type="event", include_in_digest=True,
    must_facts=["Это пересказ пилота «Маяка» в Перми."], forbidden_claims=[])
add("D203", "D", 101, "rss", ["fixed"], "Оператор «Факел» выбрал голосовой поиск для гостиниц",
    "Другой оператор, «Факел», заключил контракт на голосовой поиск для 900 гостиничных номеров в Сочи. Это отдельный коммерческий проект, не связанный с пилотом «Маяка».",
    relevance="borderline", importance="medium", roles=["PR"], topic="voice_ui", object_id="EV-D2B", object_type="event", include_in_digest=False,
    risk_reasons=["similar_but_distinct_event"], must_facts=["Проект «Факела» относится к 900 гостиничным номерам в Сочи."], forbidden_claims=["Это расширение пилота «Маяка»."])

# Rare episode D3: NPA trajectory across virtual days, no stable public ID.
add("D301", "D", 130, "regulator_html", ["fixed"], "Обсуждение правил хранения журналов телевизионных приложений",
    "Опубликован проект требований к хранению технических журналов телевизионных приложений. Публичного номера на странице нет; обсуждение открыто 26 августа и завершится 10 сентября.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="logging", object_id="NPA-D3", object_type="npa", include_in_digest=True,
    risk_reasons=["npa_without_stable_id"], must_facts=["Публичный номер отсутствует.", "Обсуждение завершится 10 сентября."], forbidden_claims=["Требования утверждены."])
add("D302", "D", 150, "regulator_html", ["fixed"], "Доработаны требования к журналам приложений на подключённых экранах",
    "Через семь виртуальных дней на другой странице опубликована редакция того же проекта: термин «телевизионные приложения» заменён на «приложения на подключённых экранах», а срок хранения снижен с 24 до 12 месяцев. Связь подтверждается приложенным протоколом обсуждения от 26 августа.",
    relevance="relevant", importance="high", roles=["GR", "HEAD"], topic="logging", object_id="NPA-D3", object_type="npa", include_in_digest=True,
    risk_reasons=["same_npa_without_id", "scope_and_version_change"], must_facts=["Срок хранения снижен с 24 до 12 месяцев.", "Термин заменён на «приложения на подключённых экранах»."], forbidden_claims=["Это новый независимый проект."])
add("D303", "D", 170, "telegram", ["fixed"], "Приложения обязали хранить журналы два года",
    "Через двадцать один виртуальный день канал повторил первую редакцию и написал об обязательном хранении журналов 24 месяца. Актуальная официальная версия остаётся проектом и содержит 12 месяцев.",
    relevance="relevant", importance="medium", roles=["GR"], topic="logging", object_id="NPA-D3", object_type="npa", include_in_digest=False,
    risk_reasons=["stale_npa_repost"], must_facts=["Канал повторяет устаревший срок 24 месяца.", "Актуальная версия содержит 12 месяцев и ещё не утверждена."], forbidden_claims=["Обязанность хранить журналы два года уже действует."])

# D3 uses virtual calendar jumps rather than compressing three legal states into one day.
for item in items:
    if item["id"] == "D302":
        item["available_at"] = "2026-09-02T10:30:00+03:00"
    elif item["id"] == "D303":
        item["available_at"] = "2026-09-16T10:50:00+03:00"

# Real search commonly rediscovers publications already present in fixed feeds.
# These overlaps make hybrid coverage pay a deduplication cost instead of getting
# an artificially disjoint and therefore automatically superior result set.
for item in items:
    if item["id"] in {"A12", "A15", "B12", "B15"}:
        item["discoverability"] = ["fixed", "search"]

# Make the same semantic stream available through realistic transport envelopes.
# B1 is still responsible for proving connectors; B3 can replay these payloads
# through the already selected adapters instead of depending on the live web.
source_names_by_id = {
    "A01":"Ритейл-практика", "A02":"HoReCa AI", "A03":"Промышленный регион", "A04":"Медиарынок Daily",
    "A05":"EdTech Brief", "A06":"Consumer IoT", "A07":"Портал проектов требований", "A08":"GS Labs",
    "A09":"Телеком-закупки", "A10":"Медиафорум", "A11":"Орган стандартизации", "A12":"TV Technology",
    "A13":"Ассоциация ТВ-платформ", "A14":"GS Group", "A15":"Калининград Online", "A16":"Media Experts",
    "A17":"Портал проектов НПА", "A18":"Парламентский портал", "A19":"Регуляторика сегодня", "A20":"Муниципальные закупки",
    "A21":"Web search", "A22":"Web search",
    "B01":"Delivery Tech", "B02":"Digital Art Channel", "B03":"Retail Design", "B04":"Streaming Review",
    "B05":"Python Education", "B06":"Product Design", "B07":"Портал проектов методик", "B08":"GS Labs",
    "B09":"Региональные меры поддержки", "B10":"Security Research", "B11":"Рабочая группа по контенту", "B12":"Видеоиндустрия",
    "B13":"Правообладатели Online", "B14":"GS Labs", "B15":"Оператор Орбита", "B16":"Телеком-инсайд",
    "B17":"Портал IoT-регулирования", "B18":"Портал безопасных устройств", "B19":"Право и техника", "B20":"Форум операторов",
    "B21":"Web search", "B22":"Web search",
    "D101":"Реестр сертификации", "D102":"Срочные новости устройств", "D103":"Реестр сертификации",
    "D201":"Оператор Маяк", "D202":"Голосовые интерфейсы", "D203":"Hospitality TV",
    "D301":"Портал обсуждений приложений", "D302":"Портал обсуждений приложений", "D303":"Право в Telegram",
}
for item in items:
    item["source_name"] = source_names_by_id[item["id"]]
    if item["surface"] == "rss":
        item["transport_payload"] = {"format":"rss_item","guid":item["id"],"title":item["title"],"description":item["raw_text"],"link":item["source_url"],"pubDate":item["available_at"]}
    elif item["surface"] == "telegram":
        item["transport_payload"] = {"format":"telegram_message","channel":item["source_name"],"message_id":item["id"],"date":item["available_at"],"text":f"{item['title']}\n\n{item['raw_text']}"}
    elif item["surface"] == "search_api":
        item["transport_payload"] = {"format":"search_result","result_id":item["id"],"title":item["title"],"content":item["raw_text"],"url":item["source_url"],"published_at":item["available_at"]}
    else:
        item["transport_payload"] = {"format":"html","content_type":"text/html; charset=utf-8","body":f"<article data-fixture-id=\"{item['id']}\"><h1>{html.escape(item['title'])}</h1><time>{item['available_at']}</time><p>{html.escape(item['raw_text'])}</p></article>"}

# This is an explicit review-policy truth, not a keyword-derived heuristic.
review_required_ids = {
    "A07", "A10", "A12", "A15", "A17", "A18", "A19", "A20",
    "B07", "B09", "B10", "B11", "B12", "B16", "B17", "B18", "B19",
    "D101", "D102", "D103", "D301", "D302", "D303",
}
for row in truth:
    row["requires_review"] = row["id"] in review_required_ids


objects = [
    {"object_id":"OBJ-A07","flow_id":"A","type":"publication","member_ids":["A07"],"roles":["GR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"До 18 сентября обсуждаются требования к защищённой загрузке новых абонентских устройств; применение предложено через девять месяцев после утверждения.","must_preserve":["discussion deadline","proposal status","nine-month transition"]},
    {"object_id":"OBJ-A08","flow_id":"A","type":"publication","member_ids":["A08"],"roles":["PR","HEAD"],"importance":"medium","critical":False,"digest":True,"ideal_summary":"GS Labs открыла программу для разработчиков телевизионных приложений с документацией и тестовыми устройствами; коммерческий запуск не гарантирован.","must_preserve":["programme launch","no guaranteed commercial launch"]},
    {"object_id":"OBJ-A09","flow_id":"A","type":"publication","member_ids":["A09"],"roles":["PR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"Оператор ищет middleware и клиентское ПО для 180 тысяч приставок; заявки до 12 сентября, победитель не выбран.","must_preserve":["180k devices","12 September deadline","no winner"]},
    {"object_id":"OBJ-A10","flow_id":"A","type":"publication","member_ids":["A10"],"roles":["GR","PR"],"importance":"medium","critical":False,"digest":False,"ideal_summary":"Отраслевые эксперты предложили добровольную прозрачность рекомендаций; нормативного требования нет.","must_preserve":["voluntary proposal","not regulation"]},
    {"object_id":"EV-A1","flow_id":"A","type":"event","member_ids":["A11","A12","A13"],"roles":["GR","PR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"С 1 января 2027 года действует добровольный стандарт доступности Smart TV. СМИ ошибочно назвали его обязательным; ассоциация поддержала цели, но считает срок подготовки недостаточным.","must_preserve":["voluntary status","effective date","media conflict","association opinion"]},
    {"object_id":"EV-A2","flow_id":"A","type":"event","member_ids":["A14","A15","A16"],"roles":["PR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"GS Group запустила на платформе GS Labs пилот локального ТВ в трёх районах для 12 тысяч домохозяйств; 20 тысяч — лишь потенциальный охват, а эксперт отмечает риск экономики контента.","must_preserve":["three districts","12k current","20k potential","independent opinion"]},
    {"object_id":"NPA-A","flow_id":"A","type":"npa","member_ids":["A17","A18","A19"],"roles":["GR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"Проект 26-041 о предустановке медиаприложений внесён в парламент; актуальная редакция переносит предлагаемое применение с 1 марта на 1 сентября 2027 года. Telegram повторяет устаревшую дату.","must_preserve":["same NPA","current stage","old and new dates","stale repost"]},
    {"object_id":"OBJ-A20","flow_id":"A","type":"publication","member_ids":["A20"],"roles":["PR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"Вне постоянных источников найдено ТЗ на ПО для 45 тысяч устройств с совместимостью с двумя CAS и сроком заявок 9 сентября; соответствие GS Labs не подтверждено.","must_preserve":["search-only","two CAS","45k devices","no assumed fit"]},
    {"object_id":"OBJ-B07","flow_id":"B","type":"publication","member_ids":["B07"],"roles":["GR","HEAD"],"importance":"medium","critical":False,"digest":False,"ideal_summary":"Обсуждается отчётность ЦОД свыше 500 кВт; применимость к GS Labs неизвестна.","must_preserve":["draft","500 kW scope","unknown applicability"]},
    {"object_id":"OBJ-B08","flow_id":"B","type":"publication","member_ids":["B08"],"roles":["PR","HEAD"],"importance":"medium","critical":False,"digest":True,"ideal_summary":"GS Labs получила отраслевую награду за интерфейс интерактивного телевидения; выводов о доле рынка награда не даёт.","must_preserve":["award subject","no market share claim"]},
    {"object_id":"OBJ-B09","flow_id":"B","type":"publication","member_ids":["B09"],"roles":["GR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"До 30 сентября принимаются заявки на компенсацию до 20% затрат на пилотные партии радиоэлектроники, максимум 30 млн рублей; право GS Labs на участие нужно проверить.","must_preserve":["20 percent","30m cap","deadline","eligibility unknown"]},
    {"object_id":"OBJ-B10","flow_id":"B","type":"publication","member_ids":["B10"],"roles":["HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"В OpenView 4.1 найдена критичная уязвимость и выпущена версия 4.1.2; сначала нужно проверить, используется ли библиотека в продуктах GS Labs.","must_preserve":["affected version","fixed version","internal dependency unknown"]},
    {"object_id":"EV-B1","flow_id":"B","type":"event","member_ids":["B11","B12","B13"],"roles":["GR","PR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"Опубликована концепция пилота цифровой маркировки легального видео, а не обязательное правило. СМИ завысили статус; правообладатели поддержали тест и просят не раскрывать параметры защиты.","must_preserve":["pilot only","media overstatement","rights-holder position"]},
    {"object_id":"EV-B2","flow_id":"B","type":"event","member_ids":["B14","B15","B16"],"roles":["PR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"GS Labs и «Орбита» проводят трёхмесячный пилот облачного интерфейса для 8 тысяч пользователей; промышленное решение не принято, несмотря на слух анонимного канала.","must_preserve":["three months","8k users","pilot not contract","rumour attribution"]},
    {"object_id":"NPA-B","flow_id":"B","type":"npa","member_ids":["B17","B18","B19"],"roles":["GR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"Доработан проект правил безопасного обновления устройств из обсуждения RU-IOT-77: при новом URL добавлено хранение журнала 12 месяцев. Документ остаётся проектом, хотя СМИ называют его действующим.","must_preserve":["same NPA despite URL","12 months","still draft"]},
    {"object_id":"OBJ-B20","flow_id":"B","type":"publication","member_ids":["B20"],"roles":["PR","HEAD"],"importance":"medium","critical":False,"digest":False,"ideal_summary":"Вне постоянных источников найден протокол: четыре оператора считают отсутствие единого API телеметрии препятствием для обновления приставок; закупки не объявлены, значимость для текущей повестки остаётся пограничной.","must_preserve":["search-only","four operators","no procurement","borderline value"]},
    {"object_id":"EV-D1","flow_id":"D","type":"event","member_ids":["D101","D102","D103"],"roles":["GR","PR","HEAD"],"importance":"critical","critical":True,"digest":True,"ideal_summary":"Сертификаты SecureBoot-X 2.4 приостановлены: в течение 30 дней нужно подтвердить отсутствие версии либо представить план обновления. Устройства без 2.4 отзыву не подлежат; сообщение о всеобщем отзыве неверно.","must_preserve":["critical","30-day response","scope correction","no assumed GS Labs usage"]},
    {"object_id":"EV-D2A","flow_id":"D","type":"event","member_ids":["D201","D202"],"roles":["PR","HEAD"],"importance":"medium","critical":False,"digest":True,"ideal_summary":"«Маяк» начал в Перми двухмесячный пилот голосового управления для 3 тысяч пользователей.","must_preserve":["Perm","3k users","two months"]},
    {"object_id":"EV-D2B","flow_id":"D","type":"event","member_ids":["D203"],"roles":["PR"],"importance":"medium","critical":False,"digest":False,"ideal_summary":"Отдельный оператор «Факел» заключил контракт на голосовой поиск для 900 гостиничных номеров в Сочи.","must_preserve":["distinct event","Sochi","900 rooms"]},
    {"object_id":"NPA-D3","flow_id":"D","type":"npa","member_ids":["D301","D302","D303"],"roles":["GR","HEAD"],"importance":"high","critical":False,"digest":True,"ideal_summary":"В проекте без публичного номера область изменена на приложения на подключённых экранах, а срок хранения журналов снижен с 24 до 12 месяцев. Telegram повторяет старую редакцию как действующее правило.","must_preserve":["identity without ID","scope change","24 to 12 months","still draft"]},
]

delivery_types = {
    "EV-D1": ["urgent_alert"],
    "NPA-D3": ["npa_update"],
}
for obj in objects:
    obj["delivery_types"] = delivery_types.get(obj["object_id"], ["planned_digest"] if obj["digest"] else [])

npa_truth = [
    {"object_id":"NPA-A","current_stage":"introduced","current_version":"v2_2026-08-24","history_ids":["A17","A18"],"stale_or_secondary_ids":["A19"],"required_change":"Дата предлагаемого применения перенесена с 1 марта на 1 сентября 2027 года."},
    {"object_id":"NPA-B","current_stage":"revised_draft","current_version":"v2_12_month_log","history_ids":["B17","B18"],"stale_or_secondary_ids":["B19"],"required_change":"В новой редакции при новом URL добавлен срок хранения журнала 12 месяцев."},
    {"object_id":"NPA-D3","current_stage":"revised_draft","current_version":"v2_connected_screens_12_months","history_ids":["D301","D302"],"stale_or_secondary_ids":["D303"],"required_change":"Область изменена на приложения на подключённых экранах, срок снижен с 24 до 12 месяцев."},
]


scenarios = {
    "dataset_version": "v1-draft",
    "time_zone": "Europe/Moscow",
    "flows": {
        "A": {"kind": "typical_shift", "item_ids": [x["id"] for x in items if x["flow_id"] == "A"], "release_at": "2026-08-24T18:00:00+03:00"},
        "B": {"kind": "typical_shift", "item_ids": [x["id"] for x in items if x["flow_id"] == "B"], "release_at": "2026-08-25T18:00:00+03:00"},
    },
    "diagnostics": {
        "D1_critical": {"item_ids": ["D101", "D102", "D103"], "virtual_time": "same_day", "primary_hypotheses": ["value", "H2", "H4"]},
        "D2_event_boundary": {"item_ids": ["D201", "D202", "D203"], "virtual_time": "same_hour", "primary_hypotheses": ["H3"]},
        "D3_npa_history": {"item_ids": ["D301", "D302", "D303"], "virtual_time": ["T0", "T+7d", "T+21d"], "primary_hypotheses": ["value", "H2", "H4"]},
    },
    "modes": {
        "fixed": "Only items whose discoverability contains fixed.",
        "search": "Only items whose discoverability contains search.",
        "hybrid": "All items available in the selected flow or diagnostic episode.",
    },
}


contract = {
    "dataset_version": "v1-draft",
    "purpose": "Scenario-level E2E evaluation of the GS Labs monitoring product and product hypotheses.",
    "data_policy": {
        "generation": "Synthetic publications were authored explicitly in this working session; no external Ollama/API model or bulk generation pipeline was used. This is team-authored synthetic data, not independent real-world evidence.",
        "factual_status": "All news, organisations except GS Labs/GS Group, document IDs and events are fictional benchmark fixtures.",
        "leakage": "The system under test receives only a packet produced by prepare_input.py. Ground truth, expected objects and judge packet stay hidden.",
    },
    "required_system_output": "prediction.schema.json",
    "evaluation": {
        "deterministic": "Exact set and label comparisons only; no keyword weights, regex relevance rules or hand-tuned composite score.",
        "semantic": "Human review or an independent LLM judge using judge_prompt.txt and evidence quotes from source items.",
        "human_protocol": "H2/H4/H5 and the main value require timed human tasks; synthetic data provides a proxy, not pilot proof.",
        "aggregation": "No single total score. Report the value hypothesis, H1-H5, safety gates and acceptance separately.",
    },
    "safety_gates": [
        "No critical object missing or assigned low importance.",
        "No unsupported factual statement in a released digest.",
        "No independent opinion silently converted into fact.",
        "No NPA stage or current version replaced by a stale report.",
        "No critical object hidden from an intended recipient.",
    ],
}


prediction_schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["run_id", "scenario_id", "mode", "item_decisions", "objects", "deliveries"],
    "properties": {
        "run_id": {"type": "string"},
        "scenario_id": {"enum": ["A", "B", "D1_critical", "D2_event_boundary", "D3_npa_history"]},
        "mode": {"enum": ["fixed", "search", "hybrid"]},
        "item_decisions": {"type": "array", "items": {"type": "object", "required": ["id", "relevance", "importance", "critical", "roles", "risk_flag"], "properties": {"id":{"type":"string"},"relevance":{"enum":["relevant","borderline","irrelevant","unknown"]},"importance":{"enum":["low","medium","high","critical"]},"critical":{"type":"boolean"},"roles":{"type":"array","items":{"enum":["PR","GR","HEAD"]}},"risk_flag":{"type":"boolean"},"reason":{"type":"string"}}}},
        "objects": {"type": "array", "items": {"type": "object", "required": ["object_id", "type", "member_ids", "summary", "importance", "critical", "roles", "claims"], "properties": {"object_id":{"type":"string"},"type":{"enum":["publication","event","npa"]},"member_ids":{"type":"array","items":{"type":"string"}},"summary":{"type":"string"},"impact_on_gs_labs":{"type":"string"},"importance":{"enum":["low","medium","high","critical"]},"critical":{"type":"boolean"},"roles":{"type":"array","items":{"enum":["PR","GR","HEAD"]}},"claims":{"type":"array","minItems":1,"items":{"type":"object","required":["claim_id","text","evidence"],"properties":{"claim_id":{"type":"string"},"text":{"type":"string"},"evidence":{"type":"array","minItems":1,"items":{"type":"object","required":["source_item_id","quote"],"properties":{"source_item_id":{"type":"string"},"quote":{"type":"string"}}}}}}},"npa_state":{"type":["object","null"],"properties":{"current_stage":{"type":"string"},"current_version":{"type":"string"},"history_ids":{"type":"array","items":{"type":"string"}},"change_summary":{"type":"string"}}}}}},
        "deliveries": {"type": "array", "items": {"type": "object", "required": ["delivery_type", "recipient", "object_ids", "digest_text"], "properties": {"delivery_type":{"enum":["planned_digest","urgent_alert","npa_update"]},"recipient":{"enum":["PR","GR","HEAD"]},"object_ids":{"type":"array","items":{"type":"string"}},"digest_text":{"type":"string"}}}},
    },
}


observation_schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "required": ["run_id", "participant_id", "scenario_id", "workflow", "task", "active_seconds", "corrections", "completed"],
    "properties": {
        "run_id": {"type": "string"},
        "participant_id": {"type": "string"},
        "scenario_id": {"enum": ["A", "B", "D1_critical", "D2_event_boundary", "D3_npa_history", "UI_ACCEPTANCE"]},
        "workflow": {"enum": ["manual_baseline", "product"]},
        "task": {"enum": ["collection", "analysis", "packaging", "review_all", "review_risk_only", "flat_events", "grouped_events", "common_release", "role_release", "full_queue", "top_layer", "acceptance"]},
        "active_seconds": {"type": "number", "minimum": 0},
        "waiting_seconds": {"type": "number", "minimum": 0},
        "corrections": {"type": "integer", "minimum": 0},
        "original_opens": {"type": "integer", "minimum": 0},
        "completed": {"type": "boolean"},
        "notes": {"type": "string"}
    }
}


coverage = {
    "counts": {
        "timeline_items": len(items),
        "flow_A": sum(x["flow_id"] == "A" for x in items),
        "flow_B": sum(x["flow_id"] == "B" for x in items),
        "diagnostic_items": sum(x["flow_id"] == "D" for x in items),
        "expected_objects": len(objects),
        "critical_items": sum(x["critical"] for x in truth),
        "irrelevant_items": sum(x["relevance"] == "irrelevant" for x in truth),
        "search_only_items": sum(x["discoverability"] == ["search"] for x in items),
    },
    "hypotheses": {
        "value": {"scenarios":["A","B","D1_critical","D3_npa_history"],"evidence":"Timed baseline vs product plus safety gates; cannot be fully automated."},
        "H1": {"scenarios":["A","B"],"variants":["fixed","search","hybrid"],"decision":"Unique relevant objects versus added irrelevant work."},
        "H2": {"scenarios":["A","B","D1_critical","D3_npa_history"],"variants":["review_all","review_risk_only","no_mandatory_review"],"decision":"Human time versus residual safety failures."},
        "H3": {"scenarios":["A","B","D2_event_boundary"],"variants":["flat","exact_duplicates","events"],"decision":"Object reduction without member, fact or opinion loss."},
        "H4": {"scenarios":["A","B","D1_critical","D3_npa_history"],"variants":["one_common","critical_core_plus_role"],"decision":"Role precision and preparation effort with zero hidden critical."},
        "H5": {"scenarios":["A","B"],"variants":["full_queue","top_layer_with_drilldown"],"decision":"Timed comprehension with equivalent critical and key-fact recall."},
    },
    "non_claims": [
        "Synthetic flows do not prove real-world search prevalence.",
        "Proxy participants do not prove adoption or trust by GS Labs staff.",
        "The dataset does not re-test connector compatibility already covered by B1.",
        "The dataset does not establish industrial frequency estimates.",
    ],
}


acceptance = {
    "scenario_id": "UI_ACCEPTANCE",
    "purpose": "Pass/fail verification of mandatory product operations; not a product hypothesis.",
    "initial_fixture_ids": ["A07", "A11", "A12", "A13", "A17", "A18", "A19", "A20", "A21"],
    "manual_material": {
        "id": "MANUAL-01",
        "title": "Письмо ассоциации о сроке перехода на стандарт доступности",
        "raw_text": "Ассоциация просит увеличить переходный период добровольного стандарта доступности с шести до двенадцати месяцев. Письмо не изменяет официальный срок и является позицией ассоциации.",
        "source_url": "https://manual.example.invalid/manual-01"
    },
    "steps": [
        {"id":"AC1","action":"add_source","expected":"Источник появляется в настройках и может быть включён."},
        {"id":"AC2","action":"disable_source","expected":"Новые элементы источника не собираются, старые и происхождение не удаляются."},
        {"id":"AC3","action":"add_manual_material","input":"MANUAL-01","expected":"Материал появляется с признаком ручного добавления и сохранённым оригиналом."},
        {"id":"AC4","action":"edit_ai_result","target":"A07","fields":["title","summary","category","importance","tags"],"expected":"Правка отделена от оригинала и AI-версии."},
        {"id":"AC5","action":"hide_and_restore","target":"A21","expected":"Скрытый элемент остаётся доступным в исключённых и восстанавливается без потери истории."},
        {"id":"AC6","action":"search_and_filter","queries":["18 сентября","NPA-A","CAS"],"filters":["source","date","category","importance"],"expected":"Находятся соответствующие сохранённые материалы; ответ и измеренное время фиксируются."},
        {"id":"AC7","action":"open_original","target":"A12","expected":"Открывается сохранённый оригинал и источник, а не только AI-саммари."},
        {"id":"AC8","action":"release","recipients":["PR","GR","HEAD"],"expected":"Зафиксированы состав, версия, канал и время выпуска; critical core не теряется."}
    ]
}


judge_prompt = """You are an independent evaluator of a GS Labs monitoring-system run.\n\nInputs: (1) visible source items, (2) hidden ground truth, (3) expected objects, (4) system prediction. Never reward style, length, keyword overlap or agreement with the ideal wording. Evaluate claims by meaning and require every factual claim in released digest text to be supported by an exact source quote.\n\nReturn JSON only. For each expected object report: inclusion, correct membership, preserved required meanings, unsupported claims, lost independent opinions, importance/critical correctness, role correctness and evidence validity. Then report separately: safety_gates; value_proxy; H1; H2_prerequisites; H3; H4_proxy; H5_content_proxy. Use verdict supported, mixed, not_supported or not_tested. H2, H4, H5 and value cannot be fully supported without timed human observations; never infer user value from text quality alone. Do not produce one total score. Quote the item IDs behind every failure.\n"""

judge_response_schema = {
    "type":"object",
    "required":["object_results","safety_gates","hypothesis_results","limitations"],
    "properties":{
        "object_results":{"type":"array","items":{"type":"object","required":["expected_object_id","inclusion","membership","required_meanings","unsupported_claims","lost_positions","role_correctness","evidence_validity"],"properties":{"expected_object_id":{"type":"string"},"inclusion":{"enum":["pass","fail"]},"membership":{"enum":["pass","partial","fail"]},"required_meanings":{"enum":["pass","partial","fail"]},"unsupported_claims":{"type":"array","items":{"type":"object","required":["claim","reason","item_ids"]}},"lost_positions":{"type":"array"},"role_correctness":{"enum":["pass","partial","fail"]},"evidence_validity":{"enum":["pass","partial","fail"]}}}},
        "safety_gates":{"type":"array","items":{"type":"object","required":["gate","verdict","item_ids"],"properties":{"gate":{"type":"string"},"verdict":{"enum":["pass","fail","not_tested"]},"item_ids":{"type":"array","items":{"type":"string"}}}}},
        "hypothesis_results":{"type":"object","properties":{"value_proxy":{"enum":["supported","mixed","not_supported","not_tested"]},"H1":{"enum":["supported","mixed","not_supported","not_tested"]},"H2_prerequisites":{"enum":["supported","mixed","not_supported","not_tested"]},"H3":{"enum":["supported","mixed","not_supported","not_tested"]},"H4_proxy":{"enum":["supported","mixed","not_supported","not_tested"]},"H5_content_proxy":{"enum":["supported","mixed","not_supported","not_tested"]}}},
        "limitations":{"type":"array","items":{"type":"string"}}
    }
}


agent_task = """Process the supplied scenario timeline as the GS Labs monitoring product. Use only the visible company context and source observations. Multiple observations can point to the same canonical_item_id when a publication was discovered through both a fixed source and search; make one item decision per canonical_item_id and do not count those observations as separate publications. Classify every canonical item, form publication/event/NPA objects, preserve provenance and conflicting positions, identify uncertainty and risk, and prepare planned or urgent role-specific deliveries. Express every factual object claim separately with one or more verbatim supporting quotes. For NPA objects, return current stage, version, history item IDs and change summary. Never invent GS Labs impact: mark unknown when internal applicability cannot be established. Return one JSON object conforming to prediction.schema.json.\n"""


context = {
    "company": "GS Labs",
    "relation": "Developer of software and equipment for digital television and adjacent media technologies within GS Group.",
    "interests": ["digital television", "subscriber devices", "middleware and interfaces", "content protection CAS/DRM", "recommendation systems", "device security and updates", "broadcasting regulation", "radioelectronics support", "operator tenders and partnerships"],
    "roles": {
        "PR": "Company and market narratives, reputation, partnerships, public positions and opportunities.",
        "GR": "Draft laws, standards, regulator actions, deadlines, stages, versions and potential regulatory impact.",
        "HEAD": "High-impact company, market, security and regulatory changes; always receives confirmed critical items."},
    "uncertainty_rule": "Unknown internal facts such as a dependency or eligibility must be surfaced as a check, not invented.",
}


run_protocol = {
    "freeze_before_run": ["dataset checksums", "B1 configuration", "B2 configuration", "system prompt", "risk policy", "release policy"],
    "machine_runs": [
        {"scenario":"A","modes":["fixed","search","hybrid"],"purpose":["value proxy","H1","H3","H4 content","H5 content"]},
        {"scenario":"B","modes":["fixed","search","hybrid"],"purpose":["value proxy","H1","H3","H4 content","H5 content"]},
        {"scenario":"D1_critical","modes":["hybrid"],"purpose":["critical safety","urgent path","H2 prerequisite","H4 safety"]},
        {"scenario":"D2_event_boundary","modes":["hybrid"],"purpose":["H3 false-merge boundary"]},
        {"scenario":"D3_npa_history","modes":["hybrid"],"purpose":["NPA identity","version history","stale report resistance"]},
    ],
    "human_tasks": {
        "value": "Cross-over: operator 1 does A manually and B with product; operator 2 does the reverse. Measure collection, analysis, packaging, waiting and correction separately.",
        "H2": "On saved outputs compare review-all and review-risk-only; no-review is measured from raw output and receives no hidden correction.",
        "H3": "Use different balanced event clusters for flat and event views; measure time and recovery of unique facts and positions.",
        "H4": "Compare manual cleaning of one common release with checking PR/GR/HEAD releases; critical core must remain everywhere required.",
        "H5": "On different balanced flows ask recipient to name main events, critical items and evidence using full queue versus top layer with drill-down.",
    },
    "reporting": "Never merge machine quality, human time and product hypotheses into one number. Synthetic outcomes are preliminary proxy evidence.",
}


DATA.mkdir(parents=True, exist_ok=True)
dump_jsonl(DATA / "timeline.jsonl", items)
dump_jsonl(DATA / "ground_truth.jsonl", truth)
dump_jsonl(DATA / "object_truth.jsonl", objects)
dump_jsonl(DATA / "npa_truth.jsonl", npa_truth)
dump_json(DATA / "scenarios.json", scenarios)
dump_json(DATA / "contract.json", contract)
dump_json(DATA / "prediction.schema.json", prediction_schema)
dump_json(DATA / "judge_response.schema.json", judge_response_schema)
dump_json(DATA / "observation.schema.json", observation_schema)
dump_json(DATA / "coverage.json", coverage)
dump_json(DATA / "context_gs_labs.json", context)
dump_json(DATA / "run_protocol.json", run_protocol)
dump_json(DATA / "acceptance.json", acceptance)
(DATA / "judge_prompt.txt").write_text(judge_prompt, encoding="utf-8")
(DATA / "agent_task.txt").write_text(agent_task, encoding="utf-8")

checksum_files = [
    "acceptance.json", "agent_task.txt", "context_gs_labs.json", "contract.json", "coverage.json",
    "ground_truth.jsonl", "judge_prompt.txt", "judge_response.schema.json", "npa_truth.jsonl",
    "object_truth.jsonl", "observation.schema.json", "prediction.schema.json", "run_protocol.json",
    "scenarios.json", "timeline.jsonl",
]
lines = []
for name in checksum_files:
    digest = hashlib.sha256((DATA / name).read_bytes()).hexdigest()
    lines.append(f"{digest}  {name}\n")
(DATA / "checksums.sha256").write_text("".join(lines), encoding="utf-8")

print(json.dumps(coverage["counts"], ensure_ascii=False, indent=2))
