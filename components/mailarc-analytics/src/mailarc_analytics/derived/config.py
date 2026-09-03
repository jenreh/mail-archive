"""The thresholds that decide what counts as a finding.

Every setting here is a limit, and every limit trades recall against noise in
a direction a user can feel: too low and a rebuild reports that everyone works
with everyone, too high and it reports nothing. The defaults are calibrated
against a planted corpus rather than taken from the spec's round numbers,
because two of the spec's numbers do not survive real text — the note on
:attr:`~AnalyticsConfig.simhash_max_distance` says which and by how much.

Nothing that is *state* is here. Which account, which mailbox, when the last
rebuild ran — that is SQLite. This file only knows how strict to be.
"""

from appkit_commons.configuration.base import BaseConfig
from pydantic_settings import SettingsConfigDict


class AnalyticsConfig(BaseConfig):
    """How strict the three derived analyses are, and how much they may read."""

    model_config = SettingsConfigDict(
        env_prefix="app_analytics_",
        env_file=".env",
        populate_by_name=True,
    )

    min_group_size: int = 3
    """Addresses a ``Group`` needs before it is worth a node.

    Three, because "who writes with whom repeatedly" is a question about
    groups. Two is a pair, every archive has thousands of them, and the
    ``CO_ADDRESSED`` edges already answer that question better.
    """

    min_group_messages: int = 2
    """Messages a ``Group`` needs before it is worth a node.

    One message to three people is a mail, not a working group. Two is the
    lowest number that means "again".
    """

    co_addressed_max_recipients: int = 25
    """Addressed recipients above which a message contributes no pair at all.

    One message with *k* recipients yields *k(k-1)/2* pairs, so a mail to five
    hundred people is a hundred and twenty-five thousand edges on its own. A
    mail to more than two dozen is a distribution list anyway, and "these two
    were both on the all-hands" is the noise that buries the finding rather
    than the finding. The message still counts towards its ``Group``, where one
    node absorbs the whole list.
    """

    simhash_max_distance: int = 5
    """Differing bits two bodies may have and still be the same template.

    The spec says three. Measured against real German business mail, three
    finds almost nothing: changing one month name in an eighty-word status
    report moves one to eight bits, and a planted twelve-message monthly series
    splits into groups of seven, three, one and one at ``<= 3``. At ``<= 5`` it
    comes back as one.

    Five costs candidate recall and does not cost cluster recall. Four bands
    guarantee that any pair within three bits shares a bucket; at five bits
    roughly a quarter of pairs are missed — but single linkage only needs
    ``k - 1`` of a group's ``k(k-1)/2`` pairs to hold it together, and the
    measured corpus recovered every member of both planted series.
    """

    lsh_band_bits: int = 16
    """Bits per LSH band — sixteen, so four non-overlapping bands over 64.

    The knob that must *not* be turned down to buy recall. Eight-bit bands
    guarantee candidates out to seven bits, and produce a hundred and fifty-six
    million candidate pairs at a hundred thousand messages against three
    hundred thousand for sixteen. Raise
    :attr:`simhash_max_distance` instead: the extra reach comes from chaining,
    not from more bands.
    """

    template_min_occurrences: int = 3
    """Copies a text needs before it is a template. Twice is a coincidence."""

    template_max_comparisons: int = 10_000_000
    """Hamming distances one direction's clustering may take in a rebuild.

    A band bucket of *k* distinct fingerprints costs ``k(k-1)/2`` comparisons
    and nothing bounds *k*: LSH promises that near-identical texts land
    together, not that everything landing together is near-identical. Measured
    on the vendored runtime, eight thousand *unrelated* fingerprints sharing
    one band cost 6.5 seconds and a hundred thousand would cost twenty minutes
    inside a job the UI is polling.

    A budget rather than the cap :attr:`topic_bucket_cap` is, because A2's
    reasoning does not transfer. An over-sized *topic* bucket is boilerplate by
    definition; an over-sized *template* bucket is what A3 exists to find, and
    it is also the cheap case — the same eight thousand fingerprints cost 0.18
    seconds when they really are near-duplicates, because every pair after the
    first unions is already in one component and skips its distance. A cap
    would throw away the largest true finding in an archive to bound a cost it
    does not incur.

    Spent smallest bucket first, so the four-message form is never the one that
    goes without, and charged per bucket up front so that a bucket is either
    compared whole or not at all. What the budget cannot afford is counted and
    reported, never silently skipped — and identical fingerprints are joined
    before any of it, so five thousand byte-identical newsletters are one
    template even in a bucket nothing was spent on.

    Ten million is roughly two seconds of Python. Real German business mail
    needs four orders of magnitude less: twenty thousand short bodies through
    the real ``simhash`` produced a largest bucket of ninety-six, which is
    forty-five hundred comparisons.
    """

    template_sample_length: int = 500
    """Characters of ``sample_text`` a ``Template`` node keeps.

    Enough for a human to recognise the text at a glance. The messages
    themselves are one edge away, so nothing is lost by not storing more.
    """

    template_ideal_words: int = 200
    """Body length at which brevity has fallen to half.

    Brevity is what separates a form from a letter. Two hundred words is about
    where a mail stops being something you could have templated and starts
    being something you actually wrote.
    """

    template_min_words: int = 25
    """Body length below which brevity is scaled down again.

    Without this floor the best automation candidate in any archive is the pile
    of one-word ``Danke!`` replies: they recur, they recur regularly, and they
    are as short as text gets. Measured, the floor moves twenty-eight daily
    one-word mails from 0.995 to 0.040 and puts the monthly status report back
    on top where it belongs.
    """

    frequency_saturation: int = 12
    """Occurrences at which the frequency factor is treated as full.

    A year of monthly mails is as often as anything needs to recur. Saturating
    rather than linear because §6.3 demands the comparison directly: one
    monthly status mail must beat two hundred identical newsletter arrivals,
    and an unbounded frequency term hands the ranking to the mailing list.
    """

    topic_min_score: float = 0.5
    """Accumulated signal weight at which two messages join one topic.

    Set exactly on the seam in :data:`~mailarc_analytics.derived.model.
    SIGNAL_WEIGHTS`: a shared ticket, thread or subject clears it alone, while
    a shared attachment (0.4) or a shared participant group (0.2) only clears
    it together. That is what keeps twelve monthly status reports to one
    recipient from becoming a "project".

    There is deliberately no second knob counting *how many* signals fired.
    With weights, "how many" is already "how much": requiring two would kill
    the ticket-token topic, the one §6.2 calls a fact, and requiring one would
    do nothing at all.
    """

    topic_bucket_cap: int = 200
    """Members an inverted-index bucket may have before its signal is dropped.

    A semantic guard before it is a performance guard. A ``subject_norm`` of
    "rechnung" shared by five thousand messages is boilerplate, not a piece of
    work, and joining them would produce one topic holding a fifth of the
    archive. Dropping the bucket loses a signal; keeping it loses the analysis.
    """

    topic_max_weak_pairs: int = 2_000_000
    """Ceiling on the accumulated weak-pair table for one rebuild.

    Only the weak signals, the ones that have to be added up, need a table at
    all — a signal that clears the threshold alone is a chain of unions and
    costs nothing. So when the budget runs out it is only the *suggestions*
    that degrade; the ticket and thread topics, the ones §6.2 calls facts, are
    never affected by memory pressure.
    """

    max_messages: int = 0
    """Messages one rebuild may read; zero means all of them.

    A ceiling **and** an ordering. A ``LIMIT`` without an ``ORDER BY`` returns
    an arbitrary subset in Cypher, so two capped rebuilds could read different
    messages, cluster them differently and mint different topic ids — which is
    precisely what the idempotence contract forbids. Any read that honours this
    orders by the canonical id first.
    """

    community_min_size: int = 3
    """Addresses a community needs before it is worth a node.

    :attr:`min_group_size`'s argument for the inexact question: two people who
    write to each other are a correspondence, and every archive has thousands
    of them. Three is the smallest number that is a circle.
    """

    community_max_iterations: int = 20
    """Passes ``algo.labelPropagation`` may take before it stops.

    Pinned rather than left to the procedure's own default because label
    propagation has no seed: an unconverged run is where two rebuilds over an
    unchanged graph disagree about an ambiguous node, and a fixed iteration
    count is the only thing this end can hold still. Twenty converges on every
    archive measured here; the digest key absorbs what it does not.
    """

    circle_min_share: float = 0.5
    """Share of a message's participants that must be in a community before
    the message is counted as circulating in it.

    A half, so a mail belongs to the circle most of its people are in. Lower
    and every mail to one member of a large circle joins it, which turns
    ``IN_CIRCLE`` into "was addressed to somebody" and makes the count
    meaningless.
    """

    centrality_max_edges: int = 2_000_000
    """Co-addressing pairs the Python PageRank will walk in one rebuild.

    The address ranking is power iteration in Python rather than
    ``algo.pageRank`` because ``CO_ADDRESSED`` is stored directed with the
    smaller id first: a PageRank over that arrow would rank an address by
    where its id sorts. Two million pairs times twenty iterations is twenty to
    forty seconds; beyond that the stage reports what it stepped over instead
    of stalling a job the UI is polling.
    """

    betweenness_sampling: int = 0
    """Nodes ``algo.betweenness`` samples; zero skips the call entirely.

    Off by default because nothing renders the number yet and it is the most
    expensive procedure in the set. The setting is the sampling size rather
    than a flag because the procedure refuses a size of zero outright — "do
    not call it" is the only way to spell off.
    """

    topic_keyword_count: int = 8
    """Keywords a ``Topic`` keeps. Enough to recognise a piece of work by,
    short enough to render as chips beside its label."""

    topic_keyword_members: int = 20
    """Messages per topic whose text the TF-IDF reads.

    Half the cost ceiling of the keyword stage, and the half that bounds the
    read: a topic of five hundred messages is described as well by twenty of
    them, and reading all five hundred would put the archive's text next to an
    in-process FalkorDB for no better answer.
    """

    topic_keyword_chars: int = 2000
    """Characters of each of those bodies, cut by ``left()`` in the store.

    The other half of the ceiling. Two thousand characters is a long business
    mail; what follows it is quoted history and a footer, and the cleaner has
    already taken most of that out.
    """

    tag_suggest_min_tagged: int = 2
    """Tagged members a group needs before it may suggest its untagged ones.

    One is a coincidence — somebody tagged one message that happens to share a
    thread with fifty others — and would turn the first tag on a busy thread
    into fifty suggestions.
    """

    tag_suggest_min_share: float = 0.3
    """Share of a group's members that must already wear the tag.

    The other half of the same guard, and the half that scales: two tagged
    messages out of five is a project, two out of two hundred is a mailing
    list somebody filed twice.
    """

    tag_auto_accept: bool = False
    """Whether a strong suggestion may tag its message without being asked.

    Off, and it is the one setting here that is a *decision* rather than a
    calibration: a tag is a human's word for a set of messages, and the
    annotation layer's whole argument is that nothing writes to it except a
    person. Turned on, every membership it creates is still marked
    ``TagSource.AUTO``, so what the analysis did stays visible.
    """

    tag_auto_accept_min_score: float = 0.6
    """Suggestion score at which auto-accept acts, when it is on at all.

    Above what the weakest group can produce: a community suggestion is
    weighted below this, so however many of a circle's messages are tagged, a
    circle alone never tags another one. A thread or a topic can.
    """
