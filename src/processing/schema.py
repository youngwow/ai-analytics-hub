"""The S2-S5 contract: the JSON Schema the answer must satisfy, and the checks
that actually enforce it.

The schema travels two ways — as the `format` parameter and, since `prompts.py`
embeds it, as text inside the system message. Only the second one currently does
anything against Ollama Cloud, which accepts `format` and ignores it (measured:
enums and `minItems` both violated; docs.ollama.com states cloud has no
structured outputs). So `parse_result` below is not a belt-and-braces check —
it is the enforcement.

The schema is the runtime source of truth and mirrors
`specs/001-processing-news/contracts/llm-output.schema.json` — keep both in step
when the shape changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models import ENTITY_ROLES, ITEM_TAGS, ITEM_TYPES, NPA_STATUSES, PRIORITIES

ENTITY_KEYS = ("who", "what", "when", "impact")
_MODEL_STATUSES = [s for s in NPA_STATUSES if s != "архив"]

RESULT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "type",
        "summary",
        "entities",
        "priority",
        "relevance_score",
        "reasoning",
        "tags",
        "confidence",
        "evidence_offsets",
    ],
    "properties": {
        "type": {"type": "string", "enum": list(ITEM_TYPES)},
        "npa_status": {"type": ["string", "null"], "enum": [*_MODEL_STATUSES, None]},
        "npa_key": {"type": ["string", "null"]},
        "summary": {"type": "array", "minItems": 3, "maxItems": 5, "items": {"type": "string"}},
        "entities": {
            "type": "object",
            "additionalProperties": False,
            "required": list(ENTITY_KEYS),
            "properties": {k: {"type": ["string", "null"]} for k in ENTITY_KEYS},
        },
        "priority": {"type": "string", "enum": list(PRIORITIES)},
        "relevance_score": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
        "matched_profile_facets": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string", "enum": list(ITEM_TAGS)}},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_offsets": {
            "type": "array",
            "items": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "integer"}},
        },
    },
}


class InvalidResponse(ValueError):
    """The answer parses as JSON but does not mean anything usable — retry once."""


@dataclass
class ParsedResult:
    """A validated model answer, ready to become an `Item`."""

    type: str = "news"
    summary: list[str] = field(default_factory=list)
    entities: dict[str, str | None] = field(default_factory=dict)
    priority: str = "medium"
    relevance_score: float = 0.0
    reasoning: str = ""
    confidence: float = 0.0
    tags: list[str] = field(default_factory=list)
    matched_profile_facets: list[str] = field(default_factory=list)
    evidence_offsets: list[tuple[int, int]] = field(default_factory=list)
    npa_status: str | None = None
    npa_key: str | None = None

    @property
    def title(self) -> str:
        return self.summary[0] if self.summary else ""


def _number(data: dict, key: str) -> float:
    value = data.get(key, 0)
    try:
        value = float(value)
    except (TypeError, ValueError) as e:
        raise InvalidResponse(f"{key}: ожидалось число, пришло {value!r}") from e
    if not 0 <= value <= 1:
        raise InvalidResponse(f"{key} вне диапазона [0, 1]: {value}")
    return value


def parse_result(
    data: dict, *, text_length: int = 0, min_sentences: int = 3, max_sentences: int = 5
) -> ParsedResult:
    """Validate one model answer; raise `InvalidResponse` when it cannot be used.

    Nothing upstream guarantees the shape against a cloud provider, so this
    checks both: that the fields are there and that they mean something — a
    summary of the right size, scores in range, and offsets that actually point
    into the document we sent.
    """
    if not isinstance(data, dict):
        raise InvalidResponse("ожидался объект JSON")

    kind = str(data.get("type") or "").strip()
    if kind not in ITEM_TYPES:
        raise InvalidResponse(f"type: ожидалось {list(ITEM_TYPES)}, пришло {kind!r}")

    summary = [str(s).strip() for s in data.get("summary") or [] if str(s).strip()]
    if not min_sentences <= len(summary) <= max_sentences:
        raise InvalidResponse(
            f"summary: ожидалось {min_sentences}-{max_sentences} предложений, пришло {len(summary)}"
        )

    raw_entities = data.get("entities")
    if not isinstance(raw_entities, dict):
        raise InvalidResponse("entities: ожидался объект")
    entities: dict[str, str | None] = {}
    for key in ENTITY_KEYS:
        value = raw_entities.get(key)
        text = str(value).strip() if value is not None else ""
        # "не указано" is the model guessing at a null; treat it as a null.
        entities[key] = text or None

    priority = str(data.get("priority") or "").strip()
    if priority not in PRIORITIES:
        raise InvalidResponse(f"priority: ожидалось {list(PRIORITIES)}, пришло {priority!r}")

    status = data.get("npa_status")
    status = str(status).strip() if status else None
    if status and status not in NPA_STATUSES:
        status = None  # unknown wording is not worth a retry: drop the status, keep the card
    npa_key = data.get("npa_key")
    npa_key = str(npa_key).strip() or None if npa_key else None
    if kind == "news":
        status, npa_key = None, None

    offsets: list[tuple[int, int]] = []
    for pair in data.get("evidence_offsets") or []:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            continue
        try:
            start, end = int(pair[0]), int(pair[1])
        except (TypeError, ValueError):
            continue
        if start < 0 or end <= start:
            continue
        if text_length and end > text_length:
            end = text_length
            if end <= start:
                continue
        offsets.append((start, end))

    tags = [t for t in (str(t).strip() for t in data.get("tags") or []) if t in ITEM_TAGS]
    facets = [str(f).strip() for f in data.get("matched_profile_facets") or [] if str(f).strip()]

    return ParsedResult(
        type=kind,
        summary=summary,
        entities=entities,
        priority=priority,
        relevance_score=_number(data, "relevance_score"),
        reasoning=str(data.get("reasoning") or "").strip(),
        confidence=_number(data, "confidence"),
        tags=list(dict.fromkeys(tags)),
        matched_profile_facets=facets,
        evidence_offsets=offsets,
        npa_status=status,
        npa_key=npa_key,
    )


def entity_spans(result: ParsedResult) -> list[tuple[str, str]]:
    """(role, value) pairs for the four narrative entities, skipping the nulls."""
    return [(role, value) for role, value in result.entities.items() if role in ENTITY_ROLES and value]
