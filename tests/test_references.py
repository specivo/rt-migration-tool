"""References to tickets in text: which are linked, and to what."""

from __future__ import annotations

import pytest

from specivo_rt_migration.references import TicketIndex, link_references

# 1 and 2 are in the bundle; 7 was merged into 1; 30 is in RT but another bundle.
_KNOWN = {1: ("1", True), 2: ("2", True), 7: ("1", True), 30: ("30", False)}
INDEX = TicketIndex(resolve=_KNOWN.get)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see ticket 2", "see {{issue:2}}"),
        ("see Ticket #2.", "see {{issue:2}}."),
        ("see ticket#2", "see {{issue:2}}"),
        ("RT #2 and RT#1 and RT 2", "{{issue:2}} and {{issue:1}} and {{issue:2}}"),
        ("Re: [example.com #2] Login", "Re: {{issue:2}} Login"),
        ("escaped \\[example.com \\#2\\] tag", "escaped {{issue:2}} tag"),
        ("merged: ticket 7", "merged: {{issue:1}}"),
        ("elsewhere: RT #30", "elsewhere: {{issue:30|RT #30}}"),
        ("unknown: ticket 99", "unknown: ticket 99"),
        ("no keyword: #2", "no keyword: #2"),
        ("tickets 2", "tickets 2"),
        ("ticket2 is a word", "ticket2 is a word"),
        ("art 2 is not RT", "art 2 is not RT"),
        ("rt 2 in lower case", "rt 2 in lower case"),
    ],
)
def test_rules(text: str, expected: str) -> None:
    assert link_references(text, INDEX) == expected


def test_code_links_and_urls_are_left_alone() -> None:
    text = (
        "```\nticket 2\n```\n"
        "`ticket 2` [ticket 2](https://example.com/ticket 2) <https://example.com/RT#2> "
        "https://rt.example.com/Ticket/Display.html?id=2 {{issue:1}} and ticket 2"
    )
    assert link_references(text, INDEX) == text.replace("and ticket 2", "and {{issue:2}}")


def test_bare_hash_numbers_only_when_asked() -> None:
    assert link_references("see #2, colour #123456", INDEX) == "see #2, colour #123456"
    assert link_references("see #2, colour #123456", INDEX, hash_numbers=True) == "see {{issue:2}}, colour #123456"
    assert link_references("a&#2; entity", INDEX, hash_numbers=True) == "a&#2; entity"


def test_label_never_breaks_a_placeholder() -> None:
    assert link_references("[x|y #30]", INDEX) == "{{issue:30|[x y #30]}}"
    assert link_references("[host #30]", INDEX) == "{{issue:30|[host #30]}}"
