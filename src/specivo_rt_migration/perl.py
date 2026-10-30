"""Read the Perl data RT 5.0 and later store settings in.

A setting changed through RT's web interface is kept in the ``Configurations``
table, its value written by Perl's ``Data::Dumper`` (``ContentType`` ``perl``)
or as JSON. This reads the subset ``Data::Dumper`` writes for plain data:
hashes, arrays, quoted strings, numbers and ``undef``. It never evaluates
anything; what it does not recognise is an error.
"""

from __future__ import annotations

import json
import re
from typing import Any

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<punct>=>|[{}\[\](),\\])
      | '(?P<single>(?:[^'\\]|\\.)*)'
      | "(?P<double>(?:[^"\\]|\\.)*)"
      | (?P<number>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)
      | (?P<word>[A-Za-z_][A-Za-z0-9_:]*)
    )""",
    re.VERBOSE | re.DOTALL,
)
_DOUBLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "e": "\x1b", "a": "\a", "f": "\f", "$": "$", "@": "@"}


class PerlDataError(ValueError):
    """Text that is not plain data as ``Data::Dumper`` writes it."""


def parse_setting(content: str | bytes | None, content_type: str | None) -> Any:
    """Return the value of one ``Configurations`` row, by its ``ContentType`` (MySQL hands it over as bytes)."""
    if content is None:
        return None
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    kind = (content_type or "").strip().lower()
    if kind == "application/json":
        return json.loads(content)
    if kind == "perl":
        return parse_dumper(content)
    return content


def parse_dumper(text: str) -> Any:
    """Parse one value written by ``Data::Dumper`` (``$VAR1 = ...;`` or terse)."""
    body = re.sub(r"^\s*\$VAR1\s*=\s*", "", text.strip())
    body = body.rstrip().removesuffix(";")
    tokens = _tokenise(body)
    value, position = _value(tokens, 0)
    if position != len(tokens):
        raise PerlDataError(f"unexpected {tokens[position][1]!r} after the value")
    return value


def _tokenise(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        if text[position:].strip() == "":
            break
        match = _TOKEN_RE.match(text, position)
        if match is None:
            raise PerlDataError(f"cannot read {text[position : position + 20]!r}")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        position = match.end()
    return tokens


def _value(tokens: list[tuple[str, str]], position: int) -> tuple[Any, int]:
    if position >= len(tokens):
        raise PerlDataError("the value ends early")
    kind, text = tokens[position]
    if kind == "punct" and text == "{":
        return _container(tokens, position + 1, "}", hash_=True)
    if kind == "punct" and text == "[":
        return _container(tokens, position + 1, "]", hash_=False)
    if kind == "punct" and text == "\\":
        # A reference to a scalar: \'value' or \1. The reference is dropped.
        return _value(tokens, position + 1)
    if kind == "single":
        return re.sub(r"\\([\\'])", r"\1", text), position + 1
    if kind == "double":
        return _unescape_double(text), position + 1
    if kind == "number":
        return (float(text) if any(char in text for char in ".eE") else int(text)), position + 1
    if kind == "word" and text == "undef":
        return None, position + 1
    raise PerlDataError(f"unexpected {text!r}")


def _container(tokens: list[tuple[str, str]], position: int, close: str, *, hash_: bool) -> tuple[Any, int]:
    items: list[Any] = []
    while True:
        if position >= len(tokens):
            raise PerlDataError(f"missing {close!r}")
        if tokens[position] == ("punct", close):
            position += 1
            break
        if hash_ and tokens[position][0] == "word" and position + 1 < len(tokens) and tokens[position + 1][1] == "=>":
            # An unquoted key.
            item: Any = tokens[position][1]
            position += 1
        else:
            item, position = _value(tokens, position)
        items.append(item)
        if position < len(tokens) and tokens[position][1] in (",", "=>"):
            position += 1
    if not hash_:
        return items, position
    if len(items) % 2:
        raise PerlDataError("a hash with an odd number of elements")
    return {str(items[index]): items[index + 1] for index in range(0, len(items), 2)}, position


def _unescape_double(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        escaped = match.group(1)
        if escaped.startswith("x{"):
            return chr(int(escaped[2:-1], 16))
        if escaped.startswith("x"):
            return chr(int(escaped[1:], 16))
        return _DOUBLE_ESCAPES.get(escaped, escaped)

    return re.sub(r"\\(x\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)", replace, text, flags=re.DOTALL)
