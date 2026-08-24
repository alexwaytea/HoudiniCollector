"""Houdini Collector for Houdini 21."""

__version__ = "0.3.1"


def show():
    """Open the collector window."""
    from .ui import show_window

    return show_window()
