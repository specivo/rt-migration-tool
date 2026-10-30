"""Articles (RT's knowledge base) to wiki pages, with their revisions.

**Where they go.** Every article belongs to a class. A class RT applies to
exactly one queue puts its articles in that queue's project wiki, where the
people working the queue look for them. Any other class (applied globally, to
several queues, or to none) becomes a project of its own holding only a wiki,
named after the class.

**Text.** An article's text is the values of its custom fields (by default
the one field ``Content``; RT 6.0 keeps it as HTML, earlier versions as text
that holds HTML when written in the rich text editor). A page with one field
is that field's text; with several, each field is a section headed by its
name. The article's summary is kept in the page's ``extra``.

**Revisions.** RT records every change of a field value as a transaction of
the article, naming the value it replaced and the value that replaced it.
Starting from the current values and undoing those transactions from the last
back gives the text the article had when created; replaying them gives each
later revision. Changes one person made in the same second are one revision.

Disabled articles are left out and counted, as are topics and links between
articles and tickets.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from specivo_migration_sdk import WikiPage, WikiVersion, source_ref

from specivo_rt_migration.content import ContentUnavailableError, DiskStorage, decode_content, to_text
from specivo_rt_migration.markdown import html_to_markdown, plain_to_markdown
from specivo_rt_migration.tickets import rt_time

ARTICLE_LOOKUP = "RT::Class-RT::Article"


@dataclass(frozen=True)
class Placement:
    """Where a class's articles go: a queue's project, or a project of the class's own."""

    project_ref: str
    own_project: bool


def place_classes(
    classes: Sequence[Mapping[str, Any]], object_classes: Sequence[Mapping[str, Any]], exported_queues: set[int]
) -> dict[int, Placement]:
    """Decide the project of each class (see the module's docstring)."""
    applied: dict[int, set[int]] = defaultdict(set)
    for row in object_classes:
        object_id = int(row.get("objectid") or 0)
        object_type = str(row.get("objecttype") or "")
        if object_id == 0 or object_type == "RT::System":
            applied[int(row["class"])].add(0)
        elif object_type == "RT::Queue":
            applied[int(row["class"])].add(object_id)
    placements: dict[int, Placement] = {}
    for row in classes:
        class_id = int(row["id"])
        queues = applied.get(class_id, set())
        if len(queues) == 1 and 0 not in queues:
            (queue,) = queues
            if queue in exported_queues:
                placements[class_id] = Placement(project_ref=str(queue), own_project=False)
                continue
        placements[class_id] = Placement(project_ref=class_project_ref(class_id), own_project=True)
    return placements


def class_project_ref(class_id: int) -> str:
    return source_ref("class", class_id)


@dataclass
class ArticleField:
    id: int
    name: str
    rt_type: str
    order: int


def _value_text(field: ArticleField, row: Mapping[str, Any], storage: DiskStorage | None) -> str:
    large = row.get("largecontent")
    if large is not None and len(large) > 0:
        text = to_text(decode_content(large, row.get("contentencoding"), storage), None)
    else:
        text = str(row.get("content") or "")
    html = field.rt_type == "HTML" or (row.get("contenttype") or "").lower() == "text/html" or _looks_html(text)
    return html_to_markdown(text, lambda _ref: None) if html else plain_to_markdown(text)


def _looks_html(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("<") and stripped.rstrip().endswith(">")


def render(
    values: Mapping[int, list[int]],
    rows: Mapping[int, Mapping[str, Any]],
    fields: Mapping[int, ArticleField],
    storage: DiskStorage | None,
) -> str:
    """An article's text from its values (field id -> value ids)."""
    present = [fields[fid] for fid in values if fid in fields and values[fid]]
    present.sort(key=lambda field: (field.order, field.id))
    sections: list[str] = []
    for field in present:
        texts = []
        for value_id in values[field.id]:
            row = rows.get(value_id)
            if row is None:
                continue
            try:
                texts.append(_value_text(field, row, storage))
            except ContentUnavailableError:
                texts.append("*(this value is in RT's external storage and could not be read)*")
        text = "\n\n".join(texts)
        sections.append(text if len(present) == 1 else f"## {field.name}\n\n{text}")
    return "\n\n".join(sections).strip() or "*(empty)*"


def revisions(
    article: Mapping[str, Any],
    transactions: Sequence[Mapping[str, Any]],
    values: Sequence[Mapping[str, Any]],
    fields: Mapping[int, ArticleField],
    storage: DiskStorage | None,
    user_ref: Any,
) -> list[WikiVersion]:
    """Every revision of an article, oldest first (see the module's docstring)."""
    rows = {int(row["id"]): row for row in values}
    state: dict[int, list[int]] = defaultdict(list)
    for row in sorted(values, key=lambda row: (int(row.get("sortorder") or 0), int(row["id"]))):
        if not int(row.get("disabled") or 0):
            state[int(row["customfield"])].append(int(row["id"]))

    changes = [
        txn for txn in transactions if txn.get("type") == "CustomField" and str(txn.get("field") or "").isdigit()
    ]
    changes.sort(key=lambda txn: int(txn["id"]))
    # Undo every change, newest first, to find the values the article was created with.
    initial: dict[int, list[int]] = {key: list(ids) for key, ids in state.items()}
    for txn in reversed(changes):
        _apply(initial, int(txn["field"]), remove=txn.get("newreference"), add=txn.get("oldreference"))

    article_id = int(article["id"])
    versions = [
        WikiVersion(
            source_ref=source_ref("article", article_id, "created"),
            version=1,
            text=render(initial, rows, fields, storage),
            author_ref=user_ref(article.get("creator")),
            created_at=rt_time(article.get("created")),
        )
    ]
    current = {key: list(ids) for key, ids in initial.items()}
    groups: list[list[Mapping[str, Any]]] = []
    for txn in changes:
        if (
            groups
            and groups[-1][0].get("creator") == txn.get("creator")
            and groups[-1][0].get("created") == txn.get("created")
        ):
            groups[-1].append(txn)
        else:
            groups.append([txn])
    for group in groups:
        for txn in group:
            _apply(current, int(txn["field"]), remove=txn.get("oldreference"), add=txn.get("newreference"))
        text = render(current, rows, fields, storage)
        if text == versions[-1].text:
            continue
        names = sorted({fields[int(txn["field"])].name for txn in group if int(txn["field"]) in fields})
        versions.append(
            WikiVersion(
                source_ref=source_ref("article", article_id, "txn", int(group[0]["id"])),
                version=len(versions) + 1,
                text=text,
                comments=("Changed " + ", ".join(names))[:1024] if names else None,
                author_ref=user_ref(group[0].get("creator")),
                created_at=rt_time(group[0].get("created")),
            )
        )
    return versions


def _apply(state: dict[int, list[int]], field_id: int, *, remove: Any, add: Any) -> None:
    ids = state.setdefault(field_id, [])
    if remove and int(remove) in ids:
        ids.remove(int(remove))
    if add and int(add) not in ids:
        ids.append(int(add))


def page(
    article: Mapping[str, Any],
    *,
    project_ref: str,
    title: str,
    versions: list[WikiVersion],
    class_name: str,
) -> WikiPage:
    extra: dict[str, Any] = {"rt_article": int(article["id"]), "rt_class": class_name}
    if article.get("summary"):
        extra["summary"] = article["summary"]
    if article.get("uri"):
        extra["uri"] = article["uri"]
    return WikiPage(
        source_ref=str(article["id"]),
        project_ref=project_ref,
        title=title,
        versions=versions,
        extra=extra,
    )


def unique_title(name: str, article_id: int, taken: set[str]) -> str:
    title = " ".join(name.split())[:240] or f"Article {article_id}"
    if title.lower() in taken:
        title = f"{title} ({article_id})"
    taken.add(title.lower())
    return title
