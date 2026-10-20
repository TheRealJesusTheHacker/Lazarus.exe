"""The Lazarus window: an animated HUD over the Phase 1-4 engine.

This package is the only part of Lazarus that knows about Qt. The
core package (``lazarus``) never imports it, so the engine and its
test suite run with no display and no PySide6 installed. Import the
window through :mod:`lazarus.ui.app` (or ``python -m lazarus.ui``);
this ``__init__`` deliberately imports nothing at all.
"""
