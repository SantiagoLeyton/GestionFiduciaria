from .parser import HistoricalWorkbookParser
from .phase3_patch import apply_phase3_patch

apply_phase3_patch()

__all__ = [
    "DuplicateHistoricalImportError",
    "HistoricalWorkbookParser",
    "analyze_historical_import",
    "find_existing_historical_import",
    "finalize_historical_import",
    "store_historical_import_file",
]


def __getattr__(name):
    if name in {"DuplicateHistoricalImportError", "analyze_historical_import", "find_existing_historical_import"}:
        from . import analyzer
        return getattr(analyzer, name)
    if name in {"finalize_historical_import", "store_historical_import_file"}:
        from . import finalize
        return getattr(finalize, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
