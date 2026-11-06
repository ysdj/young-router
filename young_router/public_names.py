"""The identity of one public model name.

A public model is the name a client sends, and LiteLLM keeps one deployment
table per spelling: ``minimax-m3.1-flash`` and ``MiniMax-M3.1-Flash`` are two
groups in ``model_names``, two deployment sets, and two rows in every picker.
A client that asks in another case reaches neither, so a document that spells
one name twice serves half of it.

This module is the one answer to "is this the same public model".  The spelling
stays exactly as the document states it -- it is what a client types and what
the pane shows -- and only the comparison folds case.
"""

from __future__ import annotations

__all__ = ["public_name_key"]


def public_name_key(name: object) -> str:
    """The identity of one public model name, case-folded.

    Surrounding whitespace is not part of a name either: it is never sent, so
    two spellings that differ only in it are one group.
    """

    return str(name or "").strip().casefold()
