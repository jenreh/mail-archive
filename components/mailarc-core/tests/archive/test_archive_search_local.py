"""The search, proved against a real FalkorDB.

`test_archive_search.py` pins the compiled statements with canned rows; this
file is what makes each filter a claim about a graph: eight messages across
two accounts, three senders, a Cc mix, an attachment mix in two file types
and two UTC offsets
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

PDF = "application/pdf"
PNG = "image/png"
"""The two content types the corpus carries, so the file filter has something
to tell apart. One kind would prove only that the traversal runs."""

ATTACHED = {PDF: "SGVsbG8gUERG", PNG: "SGVsbG8gUE5H"}
"""One payload per type — different bytes, so two nodes. See :func:`part`."""


def part(content_type: str, payload: str = "SGVsbG8gUERG") -> str:
    """One attachment part, named after the type it declares.

    ``payload`` is a parameter because an ``Attachment`` node is keyed by the
    **sha256 of its bytes**: two parts carrying the same base64 are one node
    however they are labelled, so a corpus that wanted two content types out
    of one payload would silently get one. The same reason a message needing
    two attachment *edges* has to carry two different files.
    """
    name = "anhang." + content_type.rpartition("/")[2]
    return (
        f'--BOUND\r\nContent-Type: {content_type}; name="{name}"\r\n'
        f'Content-Disposition: attachment; filename="{name}"\r\n'
        f"Content-Transfer-Encoding: base64\r\n\r\n{payload}\r\n"
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
    attach: str = "",
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
            f"{body}\r\n" + part(attach, ATTACHED[attach]) + "--BOUND--\r\n"
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
            attach=PDF,
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
            attach=PNG,
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
            attach=PDF,
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
        page = reader.search_messages(SearchFilters(senders=("Anna@Example.com",)))

        assert subjects(page) == ["Nachtrag", "Angebot 3", "Angebot 2", "Angebot 1"]
        assert page.total == 4

    def test_several_senders_mean_any_of_them(self, planted, reader) -> None:
        """The only reading there is: a message has one sender, so picking two
        addresses widens the field rather than narrowing it — Anna's four and
        Erik's one, and none of Carl's three."""
        page = reader.search_messages(
            SearchFilters(senders=("anna@example.com", "erik@example.com"))
        )

        assert subjects(page) == [
            "Nachtrag",
            "Projekt Angebot Rechnung",
            "Angebot 3",
            "Angebot 2",
            "Angebot 1",
        ]
        assert page.total == 5

    def test_half_an_address_is_not_that_address(self, planted, reader) -> None:
        """The **From** field is a picker over the archive's own addresses, so
        the filter matches one whole key — a fragment matches nothing rather
        than everything it is a fragment of."""
        page = reader.search_messages(SearchFilters(senders=("anna",)))

        assert subjects(page) == []
        assert page.total == 0

    def test_a_recipient_filter_reaches_cc_as_well_as_to(self, planted, reader) -> None:
        page = reader.search_messages(SearchFilters(recipients=(DORA,)))

        assert subjects(page) == [
            "Projekt Angebot Rechnung",
            "Rechnung April",
            "Angebot 3",
            "Angebot 2",
        ]
        assert page.total == 4

    def test_all_of_them_means_the_message_reached_every_one(
        self, planted, reader
    ) -> None:
        """Bob *and* Dora — three messages carry both, in either order of To
        and Cc, and the five that carry only one of them are gone."""
        page = reader.search_messages(
            SearchFilters(recipients=(BOB, DORA), recipients_all=True)
        )

        assert subjects(page) == [
            "Projekt Angebot Rechnung",
            "Rechnung April",
            "Angebot 2",
        ]
        assert page.total == 3

    def test_any_of_them_means_the_message_reached_one(self, planted, reader) -> None:
        """The same two addresses, the other reading of the switch: every
        message in the corpus reached one or the other."""
        page = reader.search_messages(
            SearchFilters(recipients=(BOB, DORA), recipients_all=False), limit=50
        )

        assert page.total == 8

    def test_the_recipient_fan_out_never_doubles_a_message(
        self, planted, reader
    ) -> None:
        """A message with a To *and* a Cc among the picks is two pattern rows
        — the page and the total must still count it once."""
        page = reader.search_messages(
            SearchFilters(recipients=(BOB, DORA), recipients_all=False), limit=50
        )

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

    def test_a_file_type_narrows_to_the_messages_carrying_one(
        self, planted, reader
    ) -> None:
        """The claim the tri-state above cannot make: three messages carry a
        file and they do not all carry the *same* file."""
        pdfs = reader.search_messages(SearchFilters(attachment_types=(PDF,)))
        pictures = reader.search_messages(SearchFilters(attachment_types=(PNG,)))

        assert sorted(subjects(pdfs)) == ["Angebot 2", "Termin"]
        assert subjects(pictures) == ["Rechnung April"]

    def test_several_file_types_mean_any_of_them(self, planted, reader) -> None:
        """One file has one content type, so *all of these* is a question with
        no answer — the same asymmetry the sender field has."""
        page = reader.search_messages(SearchFilters(attachment_types=(PDF, PNG)))

        assert sorted(subjects(page)) == ["Angebot 2", "Rechnung April", "Termin"]

    def test_a_type_nothing_carries_narrows_to_nothing(self, planted, reader) -> None:
        """Not to everything: an unset filter and a filter nothing satisfies
        are different questions, and the form only offers types the archive
        holds precisely so a reader never asks this one."""
        page = reader.search_messages(
            SearchFilters(attachment_types=("application/zip",))
        )

        assert page.hits == ()

    def test_a_message_with_two_matching_files_is_listed_once(
        self, planted, reader, config, store, blobs
    ) -> None:
        """The traversal fans out — one row per matching file — so a message
        carrying two PDFs would be two hits without the deduplication the
        listing already does for the recipients' *any* reading."""
        raw = eml(
            9,
            sender=ANNA,
            to=BOB,
            date="Wed, 11 Mar 2026 09:00:00 +0000",
            subject="Zwei Anhaenge",
            body="beide als PDF.",
            attach=PDF,
        )
        raw = raw.replace(
            b"--BOUND--\r\n",
            part(PDF, "WndlaXRlcyBQREY=").encode() + b"--BOUND--\r\n",
        )
        with client.session(config) as graph:
            blobs.put(raw, BlobKind.MESSAGE)
            MessageArchiver(store).archive(
                graph, parse_message(raw), source(9, "7", ())
            )

        page = reader.search_messages(SearchFilters(attachment_types=(PDF,)))

        assert subjects(page).count("Zwei Anhaenge") == 1

    def test_filters_compose_and_the_result_keeps_its_labels(
        self, planted, reader
    ) -> None:
        page = reader.search_messages(
            SearchFilters(senders=("anna@example.com",), has_attachments=True)
        )

        assert subjects(page) == ["Angebot 2"]
        assert page.total == 1
        assert [one.name for one in page.hits[0].summary.labels] == ["Kunden"]

    def test_a_structured_page_cuts_after_deduplication(self, planted, reader) -> None:
        anna = SearchFilters(senders=("anna@example.com",))
        first = reader.search_messages(anna, limit=2)
        rest = reader.search_messages(anna, limit=2, offset=2)

        assert subjects(first) == ["Nachtrag", "Angebot 3"]
        assert subjects(rest) == ["Angebot 2", "Angebot 1"]
        assert first.total == rest.total == 4


class TestTheAddressesTheFormCanOffer:
    def test_senders_are_who_actually_sent_something(self, planted, reader) -> None:
        """Busiest first: Anna sent four, Carl three, Erik one. Bob and Dora
        are in this archive and are not senders, so **From** cannot offer a
        filter that would match nothing."""
        offered = reader.known_addresses()

        assert offered.senders == (
            "anna@example.com",
            "carl@firma.de",
            "erik@example.com",
        )

    def test_recipients_count_a_cc_and_never_a_sender(self, planted, reader) -> None:
        """Bob is on seven messages, Dora on four — Dora's To and Cc both
        count, and neither of them ever sent one."""
        offered = reader.known_addresses()

        assert offered.recipients == ("bob@example.com", "dora@firma.de")

    def test_every_offered_address_actually_narrows(self, planted, reader) -> None:
        """The claim the split is for: each option, picked in its own field,
        comes back with messages rather than with nothing."""
        offered = reader.known_addresses()

        for one in offered.senders:
            assert reader.search_messages(SearchFilters(senders=(one,))).total > 0
        for one in offered.recipients:
            assert reader.search_messages(SearchFilters(recipients=(one,))).total > 0


class TestTheFileTypesTheFormCanOffer:
    def test_every_type_the_archive_holds_is_offered(self, planted, reader) -> None:
        assert set(reader.known_attachments().content_types) == {PDF, PNG}

    def test_the_commonest_type_comes_first(self, planted, reader) -> None:
        """A picker cut to its first options has to show the ones worth
        showing; the corpus carries two PDFs and one picture."""
        assert reader.known_attachments().content_types[0] == PDF

    def test_every_offered_type_actually_narrows(self, planted, reader) -> None:
        """The bargain the picker strikes: an option that returns nothing
        reads as an empty archive rather than as a filter nobody can meet."""
        for content_type in reader.known_attachments().content_types:
            page = reader.search_messages(
                SearchFilters(attachment_types=(content_type,))
            )
            assert page.hits, content_type

    def test_an_archive_with_no_files_offers_nothing(self, reader, config) -> None:
        """Unplanted on purpose — no ``planted`` fixture. An empty answer and
        not an error, which is what lets the form leave the field out."""
        assert reader.known_attachments().content_types == ()


BLOB = "application/octet-stream"
"""What several mail programs send for every attachment — a shrug."""

INVOICE = "Rechnung.pdf"
"""A PDF the sender declined to type. The only thing that says *PDF* about it
is its name, and the picker has to read that or miss it."""

LOGO = "image/png"
"""The signature picture the HTML pulls in by ``cid:`` — stored like any other
part, shown by no mail client as a file."""


def filed(number: int, *, subject: str, part: str) -> bytes:
    """One message carrying exactly one MIME part, written out whole.

    Its own builder rather than :func:`eml`'s ``attach``: what these tests are
    about is the *shape* of a part — a name without a type, a Content-ID
    without a name — and that is precisely what a corpus keyed on content type
    cannot express.
    """
    return (
        "\r\n".join(
            [
                f"From: {ANNA}",
                f"To: {BOB}",
                f"Subject: {subject}",
                "Date: Mon, 09 Mar 2026 09:00:00 +0000",
                f"Message-ID: <f{number}@example.com>",
                "MIME-Version: 1.0",
                'Content-Type: multipart/mixed; boundary="BOUND"',
            ]
        )
        + "\r\n\r\n"
        + '--BOUND\r\nContent-Type: text/plain; charset="utf-8"\r\n\r\nText\r\n'
        + part
        + "--BOUND--\r\n"
    ).encode()


FILES: list[bytes] = [
    filed(
        1,
        subject="Die Rechnung",
        part=(
            f'--BOUND\r\nContent-Type: {BLOB}; name="{INVOICE}"\r\n'
            f'Content-Disposition: attachment; filename="{INVOICE}"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\nUmVjaG51bmc=\r\n"
        ),
    ),
    filed(
        2,
        subject="Das Angebot",
        part=(
            f'--BOUND\r\nContent-Type: {PDF}; name="angebot.pdf"\r\n'
            'Content-Disposition: attachment; filename="angebot.pdf"\r\n'
            "Content-Transfer-Encoding: base64\r\n\r\nQW5nZWJvdA==\r\n"
        ),
    ),
    filed(
        3,
        subject="Nur eine Signatur",
        part=(
            f"--BOUND\r\nContent-Type: {LOGO}\r\n"
            "Content-Disposition: inline\r\n"
            "Content-ID: <logo@example.com>\r\n"
            "Content-Transfer-Encoding: base64\r\n\r\nUE5H\r\n"
        ),
    ),
]
"""Three messages, one file each: a shrug with a name, a stated type, and a
logo that is neither."""


@pytest.fixture
def files(config: GraphConfig, store: ArchiveConfig, blobs: BlobStore) -> None:
    """:func:`planted`'s corpus is eight messages every other test counts, so
    the file shapes get a graph of their own rather than shifting those."""
    archiver = MessageArchiver(store)
    with client.session(config) as graph:
        graph.execute(FULLTEXT_INDEX)
        for number, raw in enumerate(FILES, start=1):
            blobs.put(raw, BlobKind.MESSAGE)
            archiver.archive(graph, parse_message(raw), source(number, "7", ()))


class TestTheFilesTheFormCanOffer:
    """What the picker may show, on an archive holding the three shapes a real
    one holds. The rule under all of it: the picker offers exactly what the
    filter can find, because both are built from the same two lists."""

    def test_a_stated_type_is_offered_as_a_type(self, files, reader) -> None:
        assert reader.known_attachments().content_types == (PDF,)

    def test_a_shrug_is_not_offered_as_a_type(self, files, reader) -> None:
        """It would be one option standing for a folder of unrelated files —
        and picking it would match every untyped file in the archive, which is
        the one answer no picked family means."""
        assert BLOB not in reader.known_attachments().content_types

    def test_a_shrug_is_offered_under_its_name(self, files, reader) -> None:
        assert reader.known_attachments().untyped_names == (INVOICE,)

    def test_a_cid_logo_is_offered_as_neither(self, files, reader) -> None:
        """No mail client shows a paperclip for it, and
        ``Message.has_attachments`` already counts it out; a picker that
        offered *Image* here would be offering the signature of every
        newsletter in the archive."""
        offered = reader.known_attachments()

        assert LOGO not in offered.content_types
        assert offered.untyped_names == (INVOICE,)

    def test_every_offered_name_actually_narrows(self, files, reader) -> None:
        """The bargain the types already keep, kept by the other list too."""
        for name in reader.known_attachments().untyped_names:
            assert reader.search_messages(SearchFilters(attachment_names=(name,))).hits


class TestTheFileFilterItself:
    def test_a_name_finds_the_message_carrying_that_file(self, files, reader) -> None:
        page = reader.search_messages(SearchFilters(attachment_names=(INVOICE,)))

        assert subjects(page) == ["Die Rechnung"]

    def test_a_type_and_a_name_are_one_pick_widened(self, files, reader) -> None:
        """What picking **PDF** on the form expands into: the archive's PDF
        content type *and* the name of the PDF nobody typed. ORed, so both
        messages come back — the whole point of the second list."""
        page = reader.search_messages(
            SearchFilters(attachment_types=(PDF,), attachment_names=(INVOICE,))
        )

        assert subjects(page) == ["Die Rechnung", "Das Angebot"]

    def test_a_type_alone_still_means_only_that_type(self, files, reader) -> None:
        page = reader.search_messages(SearchFilters(attachment_types=(PDF,)))

        assert subjects(page) == ["Das Angebot"]

    def test_a_cid_logo_cannot_be_filtered_for(self, files, reader) -> None:
        """Asked for directly — which the form cannot do, since it is never
        offered — the message still does not come back: the statement drops
        the embedded part rather than the picker merely hiding it."""
        page = reader.search_messages(SearchFilters(attachment_types=(LOGO,)))

        assert subjects(page) == []

    def test_a_name_nothing_carries_finds_nothing(self, files, reader) -> None:
        page = reader.search_messages(SearchFilters(attachment_names=("nichts.pdf",)))

        assert subjects(page) == []


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
        copied = reader.search_messages(
            SearchFilters(text="Angebot", recipients=(DORA,))
        )
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
        page = reader.search_messages(
            SearchFilters(text="Angebot", senders=("anna@example.com",))
        )

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


class TestTheDirectoryBehindTheAddresses:
    """A different question from what the pickers offer: not *which addresses
    may I filter by* but *who is that*. Only a name can answer the second, and
    a name is what somebody has when they do not know the address."""

    def test_it_carries_the_name_an_address_signs_itself(self, planted, reader) -> None:
        """The whole reason this read exists. A question can say "von Anna
        Bauer", and ``SearchFilters.senders`` is matched exactly — so without
        the display name such a question could resolve to nothing."""
        found = {one.address: one for one in reader.address_directory().entries}

        assert found["anna@example.com"].names == ("Anna Bauer",)
        assert found["carl@firma.de"].names == ("Carl Winter",)

    def test_an_address_nobody_ever_named_still_appears(self, planted, reader) -> None:
        """Bob and Dora are only ever recipients and are written with no
        display name. An empty tuple is a name nobody gave, not a missing
        entry — leaving them out would make them unfindable by address too."""
        found = {one.address: one for one in reader.address_directory().entries}

        assert found["bob@example.com"].names == ()
        assert found["dora@firma.de"].domain == "firma.de"

    def test_it_holds_every_address_in_either_role(self, planted, reader) -> None:
        """One scan of the label, so senders and recipients are one list —
        which is what makes it cheap enough to read beside the two traversals
        ``known_addresses`` already costs."""
        offered = reader.known_addresses()
        directory = {one.address for one in reader.address_directory().entries}

        assert set(offered.senders) <= directory
        assert set(offered.recipients) <= directory

    def test_it_comes_back_in_a_stable_order(self, planted, reader) -> None:
        """Two runs over an unchanged archive have to agree, or a picker
        filled from it reshuffles under whoever is reading."""
        first = reader.address_directory().entries

        assert [one.address for one in first] == sorted(one.address for one in first)
        assert first == reader.address_directory().entries

    def test_a_role_is_the_join_of_the_two_reads(self, planted, reader) -> None:
        """How a caller gets back to "who may **From** offer, and what are
        they called": the flat directory, narrowed by the role list. Order is
        the role's own — busiest first — because that is the useful one."""
        offered = reader.known_addresses()
        senders = reader.address_directory().named(offered.senders)

        assert [one.address for one in senders] == list(offered.senders)
        assert senders[0].names == ("Anna Bauer",)

    def test_the_join_invents_nothing(self, planted, reader) -> None:
        """An address with no entry is skipped rather than made up. It cannot
        happen while both come off the same graph, and it would be a silent
        lie if it ever did."""
        directory = reader.address_directory()

        assert directory.named(("nobody@nowhere.example",)) == ()
