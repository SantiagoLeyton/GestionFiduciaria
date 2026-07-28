from .parser import HistoricalWorkbookParser

__all__ = [
    "DuplicateHistoricalImportError",
    "HistoricalWorkbookParser",
    "analyze_historical_import",
    "find_existing_historical_import",
]


def __getattr__(name):
    if name in {"DuplicateHistoricalImportError", "analyze_historical_import", "find_existing_historical_import"}:
        from . import analyzer

        return getattr(analyzer, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
