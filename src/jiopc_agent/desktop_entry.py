"""Minimal freedesktop ``.desktop`` parser (no PyXDG dependency).

Implements just what Parts B and C need from the Desktop Entry
specification:

* :func:`parse` — read the ``[Desktop Entry]`` section as a key/value dict
  (comments and blank lines skipped, localised keys like ``Name[hi]`` ignored).
* :func:`exec_argv` — split the ``Exec=`` line into an argv list, stripping
  ``%``-field codes (``%u``, ``%U``, ``%f``, ``%F``, ...) per the spec and
  unescaping ``%%`` to a literal ``%``.
* :func:`categories` — the ``Categories=`` value as a set of strings.
"""

from __future__ import annotations

import shlex
from pathlib import Path

#: Field-code letters defined by the Desktop Entry spec (``%f``, ``%U``, ...).
#: All are stripped from Exec= — the agent never substitutes files/URLs.
_FIELD_CODES = frozenset("fFuUdDnNickvm")

_MAIN_SECTION = "[Desktop Entry]"


def parse(path: Path | str) -> dict[str, str]:
    """Parse the ``[Desktop Entry]`` section of ``path`` into a dict.

    INI-ish, per the freedesktop spec subset we need: ``key=value`` lines,
    ``#`` comments and blank lines skipped, parsing confined to the
    ``[Desktop Entry]`` section, localised keys (``Key[locale]=``) ignored.
    Raises ``OSError`` if the file cannot be read (callers turn that into
    a MISSING/ERROR record).
    """
    entries: dict[str, str] = {}
    in_main = False
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):  # section header
            in_main = line == _MAIN_SECTION
            continue
        if not in_main or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or "[" in key:  # localised key, e.g. Name[hi] — ignored
            continue
        entries.setdefault(key, value.strip())
    return entries


def exec_argv(entry: dict[str, str]) -> list[str]:
    """Return the ``Exec=`` command of a parsed entry as an argv list.

    Quoting is handled with :func:`shlex.split` (a faithful superset of the
    spec's double-quote rules for real-world entries). ``%%`` unescapes to a
    literal ``%``; all other ``%X`` field codes are stripped. Tokens that
    were *only* a field code (e.g. a trailing ``%U``) are dropped entirely.
    """
    raw = entry.get("Exec", "").strip()
    if not raw:
        return []
    try:
        tokens = shlex.split(raw)
    except ValueError:  # unbalanced quotes — degrade to whitespace split
        tokens = raw.split()

    argv: list[str] = []
    for token in tokens:
        cleaned: list[str] = []
        i = 0
        while i < len(token):
            ch = token[i]
            if ch == "%" and i + 1 < len(token):
                nxt = token[i + 1]
                if nxt == "%":
                    cleaned.append("%")
                    i += 2
                    continue
                if nxt in _FIELD_CODES:
                    i += 2  # strip the field code
                    continue
            cleaned.append(ch)
            i += 1
        result = "".join(cleaned)
        if result:
            argv.append(result)
    return argv


def categories(entry: dict[str, str]) -> set[str]:
    """Return the ``Categories=`` value as a set (``;``-separated per spec)."""
    return {c.strip() for c in entry.get("Categories", "").split(";") if c.strip()}
