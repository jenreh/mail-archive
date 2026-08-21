"""Tests for :mod:`mailarc_core.mail.rendering`.

Byte-literal fixtures, like the parser's own: what is proved here is which
body a client gets, that an inline image survives the trip as a data URI, and
that a broken body is an absent one rather than an exception.
"""

import base64
from datetime import UTC, datetime

from mailarc_core.mail.model import ParsedAttachment
from mailarc_core.mail.rendering import (
    INLINE_LIMIT,
    inline_cid_images,
    render_message,
)

PNG = b"\x89PNG\r\n\x1a\nnot really a picture"

PLAIN = b"""\
From: Anna Bauer <anna@example.com>
To: Bob Baker <bob@example.com>
Cc: carl@example.com
Subject: Angebot Q3
Date: Wed, 04 Mar 2026 09:15:00 +0000
Message-ID: <plain@example.com>
Content-Type: text/plain; charset="utf-8"

Hallo Bob,

anbei das Angebot.
"""

HTML = (
    b"""\
From: Anna Bauer <anna@example.com>
To: bob@example.com
Subject: Mit Bild
Date: Wed, 04 Mar 2026 09:15:00 +0000
Message-ID: <html@example.com>
MIME-Version: 1.0
Content-Type: multipart/related; boundary="REL"

--REL
Content-Type: multipart/alternative; boundary="ALT"

--ALT
Content-Type: text/plain; charset="utf-8"

Hallo Bob, siehe Bild.

--ALT
Content-Type: text/html; charset="utf-8"

<html><body><p>Hallo <b>Bob</b></p><img src="cid:logo@example.com"><img src="CID:Missing"></body></html>

--ALT--

--REL
Content-Type: image/png; name="logo.png"
Content-ID: <logo@example.com>
Content-Disposition: inline; filename="logo.png"
Content-Transfer-Encoding: base64

"""
    + base64.encodebytes(PNG)
    + b"""
--REL--
"""
)

BROKEN = b"""\
From: anna@example.com
Subject: kaputt
Content-Type: text/html; charset="no-such-charset"

<p>Gr\xfc\xdfe</p>
"""


class TestRendering:
    def test_a_plain_mail_has_headers_and_text_but_no_html(self) -> None:
        rendered = render_message(PLAIN)

        assert rendered.subject == "Angebot Q3"
        assert rendered.sender is not None
        assert rendered.sender.display_name == "Anna Bauer"
        assert [one.address for one in rendered.to] == ["bob@example.com"]
        assert [one.address for one in rendered.cc] == ["carl@example.com"]
        assert rendered.sent_at == datetime(2026, 3, 4, 9, 15, tzinfo=UTC)
        assert rendered.body_html is None
        assert rendered.body_text.startswith("Hallo Bob,")
        assert rendered.attachments == ()

    def test_an_html_mail_keeps_its_markup_with_the_image_inlined(self) -> None:
        rendered = render_message(HTML)

        assert rendered.body_html is not None
        assert "<b>Bob</b>" in rendered.body_html
        encoded = base64.b64encode(PNG).decode()
        assert f'src="data:image/png;base64,{encoded}"' in rendered.body_html
        # A reference nothing answers is left alone, whatever its case.
        assert 'src="CID:Missing"' in rendered.body_html
        # The text alternative is still there for a client that wants it.
        assert rendered.body_text.startswith("Hallo Bob, siehe Bild.")
        assert [one.filename for one in rendered.attachments] == ["logo.png"]

    def test_an_undecodable_body_is_still_rendered_leniently(self) -> None:
        rendered = render_message(BROKEN)

        assert rendered.body_html is not None
        assert "Gr��e" in rendered.body_html


class TestInliningImages:
    def _attachment(self, **overrides) -> ParsedAttachment:
        fields = {
            "filename": "logo.png",
            "content_type": "image/png",
            "size": len(PNG),
            "sha256": "abc",
            "content_id": "logo@example.com",
            "inline": True,
            "payload": PNG,
        }
        return ParsedAttachment(**{**fields, **overrides})

    def test_single_and_double_quoted_references_are_both_replaced(self) -> None:
        html = "<img src='cid:logo@example.com'><img src=\"cid:LOGO@EXAMPLE.COM\">"

        out = inline_cid_images(html, (self._attachment(),))

        assert out.count("data:image/png;base64,") == 2
        assert "cid:" not in out

    def test_without_a_matching_attachment_the_markup_is_untouched(self) -> None:
        html = '<img src="cid:other">'

        assert inline_cid_images(html, (self._attachment(),)) == html
        assert inline_cid_images(html, ()) == html

    def test_an_attachment_past_the_limit_is_not_inlined(self) -> None:
        html = '<img src="cid:logo@example.com">'
        big = self._attachment(payload=b"x" * (INLINE_LIMIT + 1))

        assert inline_cid_images(html, (big,)) == html
