"""Prune upstream development and standalone app directories from the copied Core."""

from collections.abc import Callable
from pathlib import Path
import shutil


def _remove_directory(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def prune_our_free_model(
    root: Path, *, remove: Callable[[Path], None] = _remove_directory
) -> None:
    # The worker imports the package's core directly from src. Keep that tree
    # (including assets loaded at runtime), peers, and vendor files intact here.
    for name in ("docs", "scripts", "packages"):
        remove(root / name)
