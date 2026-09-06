from src.models import RawDocument, Source
from src.product.preparation import chunks_with_offsets, prepare_raw_document


def test_chunks_cover_full_original_without_overlap_or_loss():
    text = "Первое предложение. " + "Середина " * 50 + "Последнее предложение."
    chunks = chunks_with_offsets(text, 100)
    assert "".join(chunk.text for chunk in chunks) == text
    assert chunks[0].source_start == 0
    assert chunks[-1].source_end == len(text)
    assert all(left.source_end == right.source_start for left, right in zip(chunks, chunks[1:]))


def test_attachments_are_visible_but_not_opened():
    raw = RawDocument(1, "x", "https://example.test", title="PDF", attachments=["file.pdf"])
    prepared = prepare_raw_document("m1", raw, Source(name="Источник", direction="gr"))
    assert prepared.completeness == "partial"
    assert prepared.direction == "GR"
    assert "attachments are not opened in MVP" in prepared.warnings
