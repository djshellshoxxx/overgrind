"""Static factory registry: 'pkg.module:callable' strings resolve WITHOUT relying on import-by-string alone.

PyInstaller only bundles modules it can see. Importing the built-in adapter and strategy modules here
(explicitly, at module import time) makes them visible to the freezer; `resolve_callable` consults the
registry first and falls back to `importlib` for third-party factories.
"""
from __future__ import annotations

import importlib
from typing import Any

from . import scheduler as _scheduler
from .adapters import mock as _mock
from .adapters import resolve as _resolve  # noqa: F401  (static reference for freezers)
from .strategies import (  # noqa: F401  (explicit imports: the strategy factories use importlib by name)
    adaptive, boundary, chaos, ordered_pairs, state_cover, twise,
)

BUILTIN: dict[str, Any] = {
    "overgrind.adapters.mock:MockAdapter": _mock.MockAdapter,
    "overgrind.adapters.mock:simple_generator": _mock.simple_generator,
    "overgrind.scheduler:make_generator": _scheduler.make_generator,
}


def resolve_callable(path: str) -> Any:
    """Registry first, then importlib. Raises ImportError/AttributeError/ValueError like importlib."""
    if path in BUILTIN:
        return BUILTIN[path]
    mod, _, attr = path.partition(":")
    if not mod or not attr:
        raise ValueError(f"bad factory {path!r}; expected 'package.module:callable'")
    return getattr(importlib.import_module(mod), attr)
