"""Target product backend: architecture approved in project/ЦИФРА/03_* docs."""

from .analysis import PrimaryAnalyzer
from .contracts import AnalysisDraft, ContextBlock, GsLabsContext, PreparedDocument, SignalDraft

__all__ = [
    "AnalysisDraft",
    "ContextBlock",
    "GsLabsContext",
    "PreparedDocument",
    "PrimaryAnalyzer",
    "SignalDraft",
]
