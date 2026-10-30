"""HTML and plain message text to Markdown."""

from __future__ import annotations

from specivo_rt_migration.markdown import html_to_markdown, plain_to_markdown


def _none(_target: str) -> str | None:
    return None


def test_html_is_converted_and_sanitised() -> None:
    html = (
        "<h2>Steps</h2><ol><li>Open <b>Settings</b></li><li>Click <a href='https://example.com/x'>here</a></li></ol>"
        "<script>alert(1)</script><style>p{color:red}</style><p onclick='x()'>Done</p>"
        "<a href='javascript:alert(1)'>bad</a>"
    )
    markdown = html_to_markdown(html, _none)
    assert "## Steps" in markdown
    assert "1. Open **Settings**" in markdown
    assert "[here](https://example.com/x)" in markdown
    assert "alert" not in markdown and "color" not in markdown and "onclick" not in markdown
    assert "javascript" not in markdown
    assert markdown.endswith("Done\n\nbad") or "bad" in markdown


def test_inline_images_become_attachment_placeholders() -> None:
    refs = {"img1@example.com": "41", "77": "77"}
    html = '<p><img src="cid:img1@example.com" alt="the error"></p><img src="/rt/Ticket/Attachment/12/77/a.png">'
    markdown = html_to_markdown(html, refs.get)
    assert "![the error]({{attachment:41}})" in markdown
    assert "({{attachment:77}})" in markdown


def test_a_mail_cannot_name_a_file_by_writing_the_marker() -> None:
    """Text written like the URL the conversion marks images with stays a link: no placeholder."""
    refs = {"77": "77", "img1@example.com": "41"}
    for url in (
        "https://specivo-attachment.invalid/77",
        "https://specivo-attachment&#46;invalid/77",
        "https://specivo-attachment-0123456789abcdef0123456789abcdef.invalid/77",
    ):
        html = f'<p><img src="{url}" alt="x"> <a href="{url}">file</a> <img src="cid:img1@example.com"></p>'
        markdown = html_to_markdown(html, refs.get)
        assert "{{attachment:77}}" not in markdown, url
        assert markdown.count("{{attachment:") == 1 and "({{attachment:41}})" in markdown


def test_unknown_cid_is_dropped_with_its_image() -> None:
    markdown = html_to_markdown('<p>see <img src="cid:gone@example.com" alt="x"></p>', _none)
    assert "cid:" not in markdown
    assert markdown == "see"


def test_text_that_looks_like_a_placeholder_is_escaped() -> None:
    assert html_to_markdown("<p>{{issue:5}}</p>", _none) == "{\\{issue:5}}"
    assert plain_to_markdown("{{user:1}}") == "{\\{user:1}}"


def test_tags_in_html_text_stay_text() -> None:
    assert html_to_markdown("<p>use &lt;b&gt; for bold</p>", _none) == "use &lt;b> for bold"


def test_plain_text_keeps_lines_and_escapes_markup() -> None:
    text = "Hello *team*,\n# not a heading\n1. not a list\n  indented\n\n> quoted\n<b>raw</b> [x](y)"
    assert plain_to_markdown(text) == (
        "Hello \\*team\\*,\\\n"
        "\\# not a heading\\\n"
        "1\\. not a list\\\n"
        "&nbsp;&nbsp;indented\n"
        "\n"
        "\\> quoted\\\n"
        "\\<b\\>raw\\</b\\> \\[x\\](y)"
    )


def test_plain_thai_text() -> None:
    assert plain_to_markdown("สวัสดีครับ\r\nขอบคุณ\r\n") == "สวัสดีครับ\\\nขอบคุณ"
