"""Evidence coordinates are computed over the exact text seen by the model."""

import pytest

from src.processing import normalize, prompts


@pytest.mark.parametrize("text, rows", [
    ("Ёж 🦔. Закон принят.", ["[0,5] Ёж 🦔.", "[6,19] Закон принят."]),
    ("Повтор.\nПовтор.", ["[0,7] Повтор.", "[8,15] Повтор."]),
    ("  Закон принят.  ", ["[2,15] Закон принят."]),
    ("", []),
])
def test_evidence_table_uses_character_coordinates(text, rows):
    prompt = prompts.build(text=text)
    document, table = prompt.split("Готовые координаты предложений исходного текста", 1)
    assert text in document
    assert table.splitlines()[1:] == rows


def test_clipped_document_has_no_offsets_into_omitted_text():
    text = normalize.clip("Закон принят. " * 100, 50)
    prompt = prompts.build(text=text)
    table = prompt.split("Готовые координаты предложений исходного текста", 1)[1]
    for row in table.splitlines()[1:]:
        offsets, sentence = row.split("] ", 1)
        start, end = map(int, offsets[1:].split(","))
        assert 0 <= start < end <= len(text)
        assert text[start:end] == sentence
