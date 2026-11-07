"""The identity of one public model name, and the spelling an import files.

A public model is the name a client sends, and the name *is* the identity:
``minimax-m3.1-flash`` and ``MiniMax-M3.1-Flash`` are two models, two deployment
sets in LiteLLM, and two rows in every picker.  Nothing folds one onto the
other.  The case is settled once, where a model enters the document: an import
files the name it read, as lower case unless the user answered that it should
keep the catalog's own spelling (``imported_public_name``).  After that the
document is the authority on the spelling -- a rename is the user's -- and no
reader re-spells a name behind their back.
"""

from __future__ import annotations

__all__ = ["IMPORTED_NAME_CASES", "import_name_case", "imported_public_name", "public_name_key"]

#: The two spellings an import may file a name under.  ``lower`` is what a
#: roster is filed as unless the person importing it said otherwise: a client
#: types one spelling, and a catalog label is a label for people.  ``original``
#: keeps exactly what the upstream stated, which is the answer when the station's
#: own spelling is the one that must reach the client.
IMPORTED_NAME_CASES = ("lower", "original")


def public_name_key(name: object) -> str:
    """The name a document serves, minus surrounding whitespace.

    Surrounding whitespace is not part of a name -- it is never sent, so two
    spellings that differ only in it are one name.  Case is part of the name:
    the import already stated which spelling a model is filed under.
    """

    return str(name or "").strip()


def import_name_case(value: object = "lower") -> str:
    """Read one import's spelling decision; anything but ``original`` is lower.

    The answer arrives from the question an import asks when the names it is
    about to file contain upper case (``model.add_many`` rows carry it, a
    catalog pick states it once for the whole pick).  A caller that states
    nothing -- a path with no surface to ask on -- gets the default.
    """

    return "original" if str(value or "").strip().lower() == "original" else "lower"


def imported_public_name(name: object, *, name_case: object = "lower") -> str:
    """The public name an import files, out of the spelling it read.

    A catalog's capitalisation is a label for people, not a name a client
    should have to match, so the default is lower case.  ``name_case`` is the
    user's answer to the question the import asked when the roster contained
    upper case: ``original`` files exactly what the upstream stated.  A row the
    user names (a rename, a typed upstream) is not an import and keeps what they
    wrote.
    """

    text = str(name or "").strip()
    return text if import_name_case(name_case) == "original" else text.lower()
