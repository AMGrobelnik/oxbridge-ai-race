"""Finds the files that modules add to agent/commands/ and agent/routes/.

Each module adds features by dropping in a new file, so adding a feature never means
editing the files that load them.
"""

import importlib
import pkgutil
from types import ModuleType


def load_plugins(package: ModuleType) -> list[ModuleType]:
    """Imports every module in `package` (alphabetically), skipping names that start with _."""
    names = sorted(m.name for m in pkgutil.iter_modules(package.__path__) if not m.name.startswith("_"))
    return [importlib.import_module(f"{package.__name__}.{name}") for name in names]
