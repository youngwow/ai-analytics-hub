"""src/processing/normalize.py — S0: what the model reads and where its offsets point."""

from __future__ import annotations

import pytest

from src.processing.normalize import chunks, clip, lead, normalize, split_sentences

# Two lines, two sentences each: enough to exercise the per-line block logic.
SAMPLE = (
    "Минцифры внесло законопроект. Документ вступит в силу 1 января 2027 года!\n"
    "Кто отвечает за аккредитацию? «Профильный комитет», — уточнили в ведомстве."
)
SAMPLE_SENTENCES = [
    "Минцифры внесло законопроект.",
    "Документ вступит в силу 1 января 2027 года!",
    "Кто отвечает за аккредитацию?",
    "«Профильный комитет», — уточнили в ведомстве.",
]


# ── normalize ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "\t \n \t"], ids=["empty", "spaces", "newlines", "mixed"])
def test_normalize_of_blank_text_is_empty(text):
    assert normalize(text) == ""


def test_normalize_strips_tags_and_unescapes_entities(fixture_bytes):
    raw = fixture_bytes("processing/article_raw.html").decode("utf-8")
    result = normalize(raw)
    assert "<p>" not in result and "</div>" not in result
    assert "&nbsp;" not in result and "&laquo;" not in result
    assert "Минцифры внесло в правительство законопроект № 112233-8" in result
    assert "«Требование распространяется на компании из реестра отечественного ПО»" in result


@pytest.mark.parametrize(
    "line",
    [
        "Читайте также: как ИТ-компании готовятся к аккредитации",
        "Читайте по теме: реестр отечественного ПО",
        "Материалы по теме",
        "Подписывайтесь на наш Telegram-канал",
        "Подпишитесь на рассылку",
        "Поделиться в социальных сетях",
        "Реклама. ООО «Рекламодатель»",
        "Фото: пресс-служба Минцифры",
        "фото : РИА Новости",
        "Иллюстрация: Freepik",
        "Источник: ТАСС",
        "Теги: ИИ, регулирование",
        "Все новости раздела",
        "Смотрите также",
        "Нашли ошибку в тексте?",
    ],
    ids=lambda s: s[:20],
)
def test_normalize_drops_boilerplate_lines(line):
    body = "Минцифры внесло законопроект.\n" + line + "\nДокумент вступит в силу."
    assert normalize(body) == "Минцифры внесло законопроект.\nДокумент вступит в силу."


def test_normalize_keeps_a_line_that_only_mentions_a_boilerplate_word(fixture_bytes):
    body = "Издание пишет, что фото: сделано в студии."
    assert normalize(body) == body


def test_normalize_collapses_spaces_tabs_and_blank_lines():
    body = "Минцифры\tвнесло   законопроект.\n\n\n   Документ  вступит  в  силу.   \n"
    assert normalize(body) == "Минцифры внесло законопроект.\nДокумент вступит в силу."


def test_normalize_collapses_the_non_breaking_space_html_leaves_behind():
    assert normalize("<p>с 1&nbsp;января 2027&nbsp;года</p>") == "с 1 января 2027 года"


def test_normalize_normalises_windows_and_mac_line_endings():
    assert normalize("Первая строка.\r\nВторая строка.\rТретья строка.") == (
        "Первая строка.\nВторая строка.\nТретья строка."
    )


def test_normalize_is_idempotent_on_already_clean_text():
    once = normalize(SAMPLE)
    assert once == SAMPLE
    assert normalize(once) == once


# ── split_sentences ────────────────────────────────────────────────────────


def test_split_sentences_returns_the_sentences_in_reading_order():
    assert [s.text for s in split_sentences(SAMPLE)] == SAMPLE_SENTENCES


def test_split_sentence_offsets_slice_back_to_the_sentence():
    for sentence in split_sentences(SAMPLE):
        assert SAMPLE[sentence.start : sentence.end] == sentence.text


def test_split_sentences_offsets_are_ascending_and_inside_the_text():
    spans = split_sentences(SAMPLE)
    assert spans[0].start == 0
    assert spans[-1].end <= len(SAMPLE)
    assert all(a.end <= b.start for a, b in zip(spans, spans[1:]))


@pytest.mark.parametrize("text", ["", "   ", "\n"], ids=["empty", "spaces", "newline"])
def test_split_sentences_of_blank_text_is_empty(text):
    assert split_sentences(text) == []


def test_split_sentences_keeps_a_sentence_without_a_final_full_stop():
    text = "Минцифры внесло законопроект"
    [only] = split_sentences(text)
    assert (only.text, only.start, only.end) == (text, 0, len(text))


def test_split_sentences_does_not_split_on_a_lowercase_continuation():
    text = "Приказ вступает в силу с 1 января 2027 г. по решению правительства."
    assert [s.text for s in split_sentences(text)] == [text]


def test_split_sentences_offsets_account_for_leading_whitespace():
    text = "   Минцифры внесло законопроект. Документ подписан."
    first, second = split_sentences(text)
    assert text[first.start : first.end] == "Минцифры внесло законопроект."
    assert text[second.start : second.end] == "Документ подписан."


# ── clip ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", [len(SAMPLE), len(SAMPLE) + 1, 100000])
def test_clip_leaves_a_short_text_alone(limit):
    assert clip(SAMPLE, limit) == SAMPLE


@pytest.mark.parametrize("limit", [0, -1], ids=["zero", "negative"])
def test_clip_with_a_non_positive_limit_returns_the_text_unchanged(limit):
    assert clip(SAMPLE, limit) == SAMPLE


def test_clip_cuts_on_a_sentence_boundary_when_one_is_close():
    result = clip(SAMPLE, 40)
    assert result == "Минцифры внесло законопроект."
    assert len(result) <= 40


def test_clip_never_returns_more_than_the_limit():
    text = "А" * 50 + ". " + "Б" * 200
    result = clip(text, 100)
    assert len(result) <= 100
    assert text.startswith(result)


def test_clip_falls_back_to_a_hard_cut_when_the_boundary_is_too_early():
    # The only boundary sits at 4 % of the limit; cutting there would throw away
    # almost everything, so the prefix is kept as is.
    text = "Да. " + "слово " * 100
    result = clip(text, 100)
    assert result.startswith("Да. слово")
    assert len(result) <= 100


def test_clip_cuts_on_a_line_break_too():
    text = "Первая строка.\n" + "Б" * 300
    assert clip(text, 20) == "Первая строка."


# ── chunks ─────────────────────────────────────────────────────────────────


def test_chunks_of_empty_text_is_empty():
    assert chunks("", 100) == []


def test_chunks_returns_one_chunk_when_the_text_fits():
    assert chunks(SAMPLE, len(SAMPLE)) == [SAMPLE]


@pytest.mark.parametrize("size", [0, -5], ids=["zero", "negative"])
def test_chunks_with_a_non_positive_size_returns_the_whole_text(size):
    assert chunks(SAMPLE, size) == [SAMPLE]


def test_chunks_never_splits_a_sentence():
    parts = chunks(SAMPLE, 50)
    assert len(parts) > 1
    for sentence in SAMPLE_SENTENCES:
        assert sum(part.count(sentence) for part in parts) == 1


def test_chunks_keep_the_sentences_in_order():
    parts = chunks(SAMPLE, 50)
    joined = " ".join(parts)
    positions = [joined.index(s) for s in SAMPLE_SENTENCES]
    assert positions == sorted(positions)


def test_a_single_oversized_sentence_becomes_one_chunk():
    text = "Слово " * 60 + "конец."
    assert chunks(text, 50) == [text.strip()]


# ── lead ───────────────────────────────────────────────────────────────────


def test_lead_returns_the_first_sentences():
    assert lead(SAMPLE) == SAMPLE_SENTENCES[:3]


@pytest.mark.parametrize("count", [1, 2, 4, 10])
def test_lead_honours_the_requested_count(count):
    assert lead(SAMPLE, count) == SAMPLE_SENTENCES[:count]


def test_lead_of_blank_text_is_empty():
    assert lead("   ") == []
