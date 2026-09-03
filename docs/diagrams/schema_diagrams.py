"""The two pictures of what the archive stores.

Split out of :mod:`diagrams` rather than kept beside the other five, because
that module passed a thousand lines when the annotation layer arrived and the
repository's own rule (AGENTS §5) is that a file which does is split rather
than grown. These two belong together on their own terms: every other diagram
in the documentation is about something the application *does*, and these are
the only two about what it *holds* — the graph on one side of the process
boundary and the relational store on the other.

:data:`diagrams.ALL` is still the one list a build reads, and it still names
all seven.
"""

from render import Box, Diagram, Link

# --------------------------------------------------------------------------- #
# 1. Graph ground truth
# --------------------------------------------------------------------------- #

GRAPH_MODEL = Diagram(
    name="graph-model",
    title="The graph, in three layers",
    caption=(
        "Solid boxes are what the provider sent. The dashed frames are what a "
        "person wrote down and what an analysis worked out."
    ),
    boxes=(
        Box(
            id="message",
            label="Message",
            sub="id = canonical id",
            x=380,
            y=280,
            w=200,
            h=70,
            kind="core",
        ),
        Box(
            id="address",
            label="Address",
            sub="id = normalised address",
            x=380,
            y=40,
            w=200,
            h=62,
            kind="core",
        ),
        Box(
            id="thread",
            label="Thread",
            sub="id = account:thread",
            x=60,
            y=160,
            w=190,
            h=62,
            kind="core",
        ),
        Box(
            id="parent",
            label="Message",
            sub="the one replied to",
            x=60,
            y=280,
            w=190,
            h=62,
            kind="core",
        ),
        Box(
            id="label",
            label="Label",
            sub="id = account:name",
            x=60,
            y=400,
            w=190,
            h=62,
            kind="core",
        ),
        Box(
            id="attachment",
            label="Attachment",
            sub="id = sha256 of the file",
            x=700,
            y=280,
            w=200,
            h=62,
            kind="core",
        ),
        Box(
            id="account",
            label="Account",
            sub="id = SQLite row id",
            x=700,
            y=430,
            w=200,
            h=62,
            kind="core",
        ),
        Box(
            id="note",
            label="Properties on Message",
            sub=(
                "subject · subject_norm · sent_at · body_text · body_clean · "
                "simhash · participant_key · refs · eml_sha256 · embedding · "
                "importance · importance_reasons · importance_version"
            ),
            x=60,
            y=910,
            w=840,
            h=62,
            kind="note",
            bold=False,
        ),
        Box(
            id="g_annotation",
            label="Annotation layer",
            x=40,
            y=640,
            w=400,
            h=230,
            kind="group",
        ),
        Box(
            id="tag",
            label="Tag",
            sub="id = tag:<slug>",
            x=130,
            y=700,
            w=240,
            h=70,
            kind="accent",
        ),
        Box(
            id="tag_note",
            label="Written by a person, never by a rebuild",
            sub="TAGGED is the membership; SUGGESTED only points here",
            x=60,
            y=795,
            w=360,
            h=50,
            kind="note",
            bold=False,
        ),
        Box(
            id="g_derived",
            label="Derived layer",
            x=600,
            y=640,
            w=520,
            h=230,
            kind="group",
        ),
        Box(
            id="community",
            label="Community",
            sub="id = digest of its members",
            x=820,
            y=700,
            w=240,
            h=70,
            kind="ui",
        ),
        Box(
            id="derived_note",
            label="Group · Topic · Template are derived too",
            sub="one rebuild deletes all four and works them out again",
            x=620,
            y=795,
            w=480,
            h=50,
            kind="note",
            bold=False,
        ),
    ),
    links=(
        Link(src="message", dst="address", exit="nw", entry="sw", label="SENT_FROM"),
        Link(src="message", dst="address", exit="n", entry="s", label="SENT_TO"),
        Link(
            src="message",
            dst="address",
            exit="ne",
            entry="se",
            label="COPIED_TO / BLIND_COPIED_TO",
            label_dx=90,
        ),
        Link(src="message", dst="thread", exit="wn", entry="e", label="IN_THREAD"),
        Link(
            src="message",
            dst="parent",
            exit="w",
            entry="e",
            label="REPLIES_TO",
            label_dx=-14,
        ),
        Link(src="message", dst="label", exit="ws", entry="e", label="LABELED"),
        Link(
            src="message",
            dst="attachment",
            exit="e",
            entry="w",
            label="HAS_ATTACHMENT",
            label_dx=14,
        ),
        Link(
            src="message",
            dst="account",
            exit="es",
            entry="w",
            label="ARCHIVED_FROM",
        ),
        Link(src="message", dst="note", exit="s", entry="n", dashed=True, arrow=False),
        Link(
            src="message",
            dst="tag",
            exit="sw",
            entry="n",
            label="TAGGED",
            dashed=True,
        ),
        Link(
            src="message",
            dst="community",
            exit="se",
            entry="w",
            label="IN_CIRCLE",
            dashed=True,
        ),
        Link(
            src="address",
            dst="community",
            exit="e",
            entry="n",
            label="MEMBER_OF",
            dashed=True,
        ),
    ),
)

# --------------------------------------------------------------------------- #
# 2. Relational schema
# --------------------------------------------------------------------------- #

RELATIONAL = Diagram(
    name="relational-schema",
    title="What the relational store holds",
    caption=(
        "The graph holds what a message is. These six tables hold what we have "
        "done about it."
    ),
    boxes=(
        Box(
            id="accounts",
            label="mail_accounts",
            sub="provider · address · enabled · status",
            x=400,
            y=240,
            w=280,
            h=72,
            kind="core",
        ),
        Box(
            id="credentials",
            label="mail_credentials",
            sub="kind · secret (encrypted, opaque)",
            x=60,
            y=60,
            w=250,
            h=64,
            kind="store",
        ),
        Box(
            id="jobs",
            label="mail_sync_jobs",
            sub="kind · state · lease · counters",
            x=770,
            y=60,
            w=250,
            h=64,
            kind="sync",
        ),
        Box(
            id="checkpoints",
            label="mail_sync_checkpoints",
            sub="scope · cursor · messages_seen",
            x=60,
            y=430,
            w=250,
            h=64,
            kind="sync",
        ),
        Box(
            id="archived",
            label="mail_archived_messages",
            sub="provider id → canonical id",
            x=770,
            y=430,
            w=250,
            h=64,
            kind="accent",
        ),
        Box(
            id="failed",
            label="mail_failed_messages",
            sub="provider id · reason · detail",
            x=400,
            y=560,
            w=280,
            h=64,
            kind="external",
        ),
        Box(
            id="note",
            label="Rebuildable, not authoritative",
            sub=(
                "mail_archived_messages is a read model over the graph — the "
                "batch IN (…) the graph cannot answer cheaply."
            ),
            x=180,
            y=670,
            w=620,
            h=62,
            kind="note",
            bold=False,
        ),
    ),
    links=(
        Link(
            src="credentials", dst="accounts", exit="e", entry="wn", label="account_id"
        ),
        Link(src="jobs", dst="accounts", exit="w", entry="en", label="account_id"),
        Link(
            src="checkpoints", dst="accounts", exit="e", entry="ws", label="account_id"
        ),
        Link(src="archived", dst="accounts", exit="w", entry="es", label="account_id"),
        Link(src="failed", dst="accounts", exit="n", entry="s", label="account_id"),
        Link(src="archived", dst="note", exit="s", entry="e", dashed=True, arrow=False),
    ),
)
