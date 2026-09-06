"""Target product backend: architecture approved in project/ЦИФРА/03_* docs."""

from .analysis import PrimaryAnalyzer
from .contracts import (
    AgentRunResult,
    AnalysisDraft,
    ContextBlock,
    DraftDigest,
    GsLabsContext,
    PreparedDocument,
    SignalDraft,
)
from .runtime import ProductAgentRuntime

__all__ = [
    "AgentRunResult",
    "AnalysisDraft",
    "ContextBlock",
    "DraftDigest",
    "GsLabsContext",
    "PreparedDocument",
    "PrimaryAnalyzer",
    "ProductAgentRuntime",
    "SignalDraft",
]
