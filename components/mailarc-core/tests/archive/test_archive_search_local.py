"""The search, proved against a real FalkorDB.

`test_archive_search.py` pins the compiled statements with canned rows; this
file is what makes each filter a claim about a graph: eight messages across
two accounts, three senders, a Cc mix, an attachment mix and two UTC offsets
go in through :class:`MessageArchiver`, and every filter has to *narrow* that
corpus live — including the two behaviours only a real store can prove, the
lexicographic wall-clock date range and full-text matching composed with
structured filters and pagination. The server fixtures live in ``conftest.py``.

The full-text index is created here the way the analytics local tests create
it: the baseline migration's exact ``CALL``, issued by the fixture, because a
graph without the index answers every search with no rows at all — which is
indistinguishable from a search that stopped working.

The date bounds are naive where the ``noqa: DTZ001`` markers say so, because
that is the only shape the application ever builds: the search page's
``parse_date`` strips the offset, so a picked day means the wall-clock day the
rows show. An aware bound would test an input no page can produce.
"""

from datetime import datetime
from functools import partial

import pytest

from mailarc_core.archive.blobs import BlobStore
from mailarc_core.archive.config import ArchiveConfig
from mailarc_core.archive.model import ArchiveSource, BlobKind
from mailarc_core.archive.reader import ArchiveReader
from mailarc_core.archive.search import SearchFilters, SearchPage
from mailarc_core.archive.writer import MessageArchiver
from mailarc_core.graph import client
from mailarc_core.graph.config import GraphConfig
from mailarc_core.mail.model import LabelInfo, LabelKind, MailProvider
from mailarc_core.mail.parsing import parse_message

pytestmark = pytest.mark.graph_local

ANNA = "Anna Bauer <anna@example.com>"
CARL = "Carl Winter <carl@firma.de>"
ERIK = "Erik Ols <erik@example.com>"
BOB = "bob@example.com"
DORA = "dora@firma.de"

FULLTEXT_INDEX = (
    "CALL db.idx.fulltext.createNodeIndex('Message', 'subject', 'body_text')"
)
"""The index the baseline migration creates, issued by the planting fixture."""

ATTACHMENT = (
    '--BOUND\r\nContent-Type: application/pdf; name="anhang.pdf"\r\n'
    'Content-Disposition: attachment; filename="anhang.pdf"\r\n'
    "Content-Transfer-Encoding: base64\r\n\r\nSGVsbG8gUERG\r\n"
)


def eml(
    number: int,
    *,
    sender: str,
    to: str,
    date: str,
    subject: str,
    body: str,
    cc: str | None = None,
    attach: bool = False,
) -> bytes:
    headers = [f"From: {sender}", f"To: {to}"]
    if cc:
        headers.append(f"Cc: {cc}")
    headers += [
        f"Subject: {subject}",
        f"Date: {date}",
        f"Message-ID: <m{number}@example.com>",
        "MIME-Version: 1.0",
    ]
    if attach:
        headers.append('Content-Type: multipart/mixed; boundary="BOUND"')
        payload = (
            '--BOUND\r\nContent-Type: text/plain; charset="utf-8"\r\n\r\n'
            f"{body}\r\n" + ATTACHMENT + "--BOUND--\r\n"
        )
    else:
        headers.append('Content-Type: text/plain; charset="utf-8"')
        payload = f"{body}\r\n"
    return ("\r\n".join(headers) + "\r\n\r\n" + payload).encode()


CORPUS: list[tuple[bytes, str, tuple[LabelInfo, ...]]] = [
    (
        eml(
            1,
            sender=ANNA,
            to=BOB,
            date="Wed, 04 Mar 2026 09:15:00 +0000",
            subject="Angebot 1",
            body="Hallo Bob, anbei das Angebot Nummer eins.",
        ),
        "7",
        (),
    ),
    (
        eml(
            2,
            sender=ANNA,
            to=BOB,
            cc=DORA,
            date="Thu, 05 Mar 2026 10:00:00 +0000",
            subject="Angebot 2",
            body="Das Angebot als Anhang.",
            attach=True,
        ),
        "7",
        (
            LabelInfo(
                provider_label_id="Label_Kunden", name="Kunden", kind=LabelKind.USER
            ),
        ),
    ),
    (
        eml(
            3,
            sender=CARL,
            to=BOB,
            date="Fri, 06 Mar 2026 09:15:00 +0200",
            subject="Rechnung März",
            body="die Rechnung für den März.",
        ),
        "7",
        (),
    ),
    (
        eml(
            4,
            sender=ANNA,
            to=DORA,
            date="Fri, 06 Mar 2026 23:30:00 +0200",
            subject="Angebot 3",
            body="noch ein Angebot.",
        ),
        "8",
        (),
    ),
    (
        eml(
            5,
            sender=CARL,
            to=DORA,
            cc=BOB,
            date="Sat, 07 Mar 2026 08:00:00 +0000",
            subject="Rechnung April",
            body="Rechnung im Anhang.",
            attach=True,
        ),
        "8",
        (),
    ),
    (
        eml(
            6,
            sender=ERIK,
            to=BOB,
            cc=DORA,
            date="Sun, 08 Mar 2026 12:00:00 +0000",
            subject="Projekt Angebot Rechnung",
            body="beides zusammen.",
        ),
        "8",
        (),
    ),
    (
        eml(
            7,
            sender=ANNA,
            to=BOB,
            date="Mon, 09 Mar 2026 09:00:00 +0000",
            subject="Nachtrag",
            body="zum Angebot von gestern.",
        ),
        "7",
        (),
    ),
    (
        eml(
            8,
            sender=CARL,
            to=BOB,
            date="Tue, 10 Mar 2026 11:00:00 +0000",
            subject="Termin",
            body="Besprechung am Freitag.",
            attach=True,
        ),
        "7",
        (),
    ),
]


def source(number: int, account: str, labels: tuple[LabelInfo, ...]) -> ArchiveSource:
    return ArchiveSource(
        account_id=account,
        account_address=f"postfach{account}@example.com",
        provider=MailProvider.GMAIL,
        provider_message_id=f"g-{account}-{number}",
        labels=labels,
    )


@pytest.fixture
def store(tmp_path) -> ArchiveConfig:
    return ArchiveConfig(store_dir=tmp_path / "blobs")


@pytest.fixture
def blobs(store: ArchiveConfig) -> BlobStore:
    return BlobStore(store)


@pytest.fixture
def reader(config: GraphConfig, blobs: BlobStore) -> ArchiveReader:
    return ArchiveReader(partial(client.session, config), blobs)


@pytest.fixture
def planted(config: GraphConfig, store: ArchiveConfig, blobs: BlobStore) -> None:
    """The corpus, written the way the import writes: index first, then all
    eight messages through the archiver, bytes into the store beside them."""
    archiver = MessageArchiver(store)
    with client.session(config) as graph:
        graph.execute(FULLTEXT_INDEX)
        for number, (raw, account, labels) in enumerate(CORPUS, start=1):
            blobs.put(raw, BlobKind.MESSAGE)
            archiver.archive(graph, parse_message(raw), source(number, account, labels))


def subjects(page: SearchPage) -> list[str]:
    return [hit.summary.subject for hit in page.hits]


class TestTheStructuredFilters:
    def test_an_empty_form_is_the_recent_listing(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters())

        assert subjects(page)[:3] == ["Termin", "Nachtrag", "Projekt Angebot Rechnung"]
        assert page.total == 8
        assert all(hit.relevance is None for hit in page.hits)

    def test_a_sender_filter_narrows_to_that_address(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(sender="anna"))

        assert subjects(page) == ["Nachtrag", "Angebot 3", "Angebot 2", "Angebot 1"]
        assert page.total == 4

    def test_a_sender_domain_narrows_by_containment(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(sender="@firma.de"))

        assert subjects(page) == ["Termin", "Rechnung April", "Rechnung März"]

    def test_a_recipient_filter_reaches_cc_as_well_as_to(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(recipient="dora"))

        assert subjects(page) == [
            "Projekt Angebot Rechnung",
            "Rechnung April",
            "Angebot 3",
            "Angebot 2",
        ]
        assert page.total == 4

    def test_the_recipient_fan_out_never_doubles_a_message(
        self, planted, reader
    ) -> None:
        """``@`` matches every recipient, so a message with a To *and* a Cc
        is two pattern rows — the page and the total must still count it
        once."""
        page = reader.search_messages(SearchFilters(recipient="@"), limit=50)

        found = [hit.summary.id for hit in page.hits]
        assert len(found) == len(set(found)) == 8
        assert page.total == 8

    def test_an_account_filter_narrows_to_that_mailbox(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(account_id="8"))

        assert subjects(page) == [
            "Projekt Angebot Rechnung",
            "Rechnung April",
            "Angebot 3",
        ]
        assert page.total == 3

    def test_the_attachment_tristate_narrows_both_ways(self, planted, reader) -> None:
        with_files = reader.search_messages(SearchFilters(has_attachments=True))
        without = reader.search_messages(SearchFilters(has_attachments=False))

        assert subjects(with_files) == ["Termin", "Rechnung April", "Angebot 2"]
        assert without.total == 5
        assert "Termin" not in subjects(without)

    def test_filters_compose_and_the_result_keeps_its_labels(
        self, planted, reader
    ) -> None:
        page = reader.search_messages(
            SearchFilters(sender="anna", has_attachments=True)
        )

        assert subjects(page) == ["Angebot 2"]
        assert page.total == 1
        assert [one.name for one in page.hits[0].summary.labels] == ["Kunden"]

    def test_a_structured_page_cuts_after_deduplication(self, planted, reader) -> None:
        first = reader.search_messages(SearchFilters(sender="anna"), limit=2)
        rest = reader.search_messages(SearchFilters(sender="anna"), limit=2, offset=2)

        assert subjects(first) == ["Nachtrag", "Angebot 3"]
        assert subjects(rest) == ["Angebot 2", "Angebot 1"]
        assert first.total == rest.total == 4


class TestTheDateRange:
    def test_the_range_reads_wall_clock_like_the_listing_orders(
        self, planted, reader
    ) -> None:
        """`Rechnung März` was sent 09:15 **+0200** — 07:15 as an instant,
        outside this window; 09:15 on the wall, inside it. The stored strings
        compare lexicographically, so the wall clock wins: the documented
        margin, pinned."""
        page = reader.search_messages(
            SearchFilters(
                sent_from=datetime(2026, 3, 6, 8, 0),  # noqa: DTZ001
                sent_until=datetime(2026, 3, 6, 10, 0),  # noqa: DTZ001
            )
        )

        assert subjects(page) == ["Rechnung März"]
        assert page.total == 1

    def test_two_days_hold_exactly_their_messages(self, planted, reader) -> None:
        page = reader.search_messages(
            SearchFilters(
                sent_from=datetime(2026, 3, 6),  # noqa: DTZ001
                sent_until=datetime(2026, 3, 7, 23, 59, 59),  # noqa: DTZ001
            )
        )

        assert subjects(page) == ["Rechnung April", "Angebot 3", "Rechnung März"]

    def test_the_lower_bound_includes_its_exact_second(self, planted, reader) -> None:
        """A stored value at the bound carries its offset suffix and sorts
        *after* the naive bound string — so a message sent at the very second
        the range opens is in."""
        page = reader.search_messages(
            SearchFilters(
                sent_from=datetime(2026, 3, 4, 9, 15),  # noqa: DTZ001
                sent_until=datetime(2026, 3, 5),  # noqa: DTZ001
            )
        )

        assert subjects(page) == ["Angebot 1"]

    def test_a_half_open_range_works_from_either_end(self, planted, reader) -> None:
        since = reader.search_messages(SearchFilters(sent_from=datetime(2026, 3, 9)))  # noqa: DTZ001
        until = reader.search_messages(SearchFilters(sent_until=datetime(2026, 3, 5)))  # noqa: DTZ001

        assert subjects(since) == ["Termin", "Nachtrag"]
        assert subjects(until) == ["Angebot 1"]


class TestTheFulltext:
    def test_words_find_subjects_and_bodies(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(text="Angebot"))

        assert set(subjects(page)) == {
            "Angebot 1",
            "Angebot 2",
            "Angebot 3",
            "Projekt Angebot Rechnung",
            "Nachtrag",
        }
        assert page.total is None

    def test_the_best_hit_scores_one_and_nothing_exceeds_it(
        self, planted, reader
    ) -> None:
        page = reader.search_messages(SearchFilters(text="Rechnung"))

        scores = [hit.relevance for hit in page.hits]
        assert scores[0] == 1.0
        assert all(one is not None and 0.0 <= one <= 1.0 for one in scores)
        assert scores == sorted(scores, reverse=True)  # type: ignore[type-var]

    def test_text_composes_with_the_account_filter(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(text="Angebot", account_id="8"))

        assert set(subjects(page)) == {"Angebot 3", "Projekt Angebot Rechnung"}

    def test_text_composes_with_recipient_and_attachments(
        self, planted, reader
    ) -> None:
        copied = reader.search_messages(SearchFilters(text="Angebot", recipient="dora"))
        with_files = reader.search_messages(
            SearchFilters(text="Rechnung", has_attachments=True)
        )

        assert set(subjects(copied)) == {
            "Angebot 2",
            "Angebot 3",
            "Projekt Angebot Rechnung",
        }
        assert subjects(with_files) == ["Rechnung April"]

    def test_fulltext_pages_are_disjoint_and_complete(self, planted, reader) -> None:
        """Five hits for `Angebot`, walked two at a time: the id tiebreak on
        equal scores is what makes these three pages a partition rather than
        a shuffle."""
        pages = [
            reader.search_messages(SearchFilters(text="Angebot"), limit=2, offset=at)
            for at in (0, 2, 4)
        ]

        found = [hit.summary.id for page in pages for hit in page.hits]
        assert [len(page.hits) for page in pages] == [2, 2, 1]
        assert len(found) == len(set(found)) == 5

    def test_hits_hydrate_with_sender_and_labels(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(text="Angebot", sender="anna"))

        by_subject = {hit.summary.subject: hit.summary for hit in page.hits}
        assert set(by_subject) == {"Angebot 1", "Angebot 2", "Angebot 3", "Nachtrag"}
        assert by_subject["Angebot 2"].sender_address == "anna@example.com"
        assert [one.name for one in by_subject["Angebot 2"].labels] == ["Kunden"]


class TestMessagesByIds:
    def test_the_asked_order_comes_back_hydrated(self, planted, reader) -> None:
        found = reader.search_messages(SearchFilters(text="Rechnung"))
        ids = [hit.summary.id for hit in found.hits]

        summaries = reader.messages_by_ids(list(reversed(ids)))

        assert [one.id for one in summaries] == list(reversed(ids))
        assert all(one.subject for one in summaries)

    def test_an_unknown_id_is_left_out(self, planted, reader) -> None:
        [known] = [
            hit.summary.id
            for hit in reader.search_messages(SearchFilters(text="Termin")).hits
        ]

        summaries = reader.messages_by_ids(["missing@example.com", known])

        assert [one.id for one in summaries] == [known]
