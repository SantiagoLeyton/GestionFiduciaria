from .parser import HistoricalWorkbookParser

__all__ = ["HistoricalWorkbookParser", "analyze_historical_import"]


def __getattr__(name):
    if name == "analyze_historical_import":
        from .analyzer import analyze_historical_import

        return analyze_historical_import
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
