"""A3 — which texts get written again and again, and which are worth automating.

Lexical, never semantic. §6.3 is explicit that embeddings are the wrong tool
here: they find mail about the same *topic*, and the question is which mail has
the same *wording*. The import already answered the hard part by hashing
``body_clean`` — quotes, signature and disclaimer removed — into a 64-bit
SimHash, so this module only has to find the fingerprints that sit close
together and decide which of those groups a human would want back as a form.

**Sent and received are clustered separately.** Only what you write yourself is
automatable, so the two are two passes over two disjoint sets, and the
direction is on the node. A score is only comparable within one direction
anyway: a newsletter arriving every Tuesday is regular, frequent and short, and
none of that is anybody's to automate.

The bucketing, exactly. ``lsh_band_bits = 16`` cuts the 64-bit fingerprint into
four non-overlapping bands, band *i* covering bits ``[16i, 16i+16)`` from the
least significant end. The bucket key is the **pair** ``(band index, band
value)`` — without the index, band 0 of one message collides with band 3 of
another. Two messages are candidates when they share any bucket, and a
candidate is only kept when
:func:`~mailarc_core.mail.parsing.hamming_distance` over the *unsigned* values
is within
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.simhash_max_distance`.

With four bands, any two fingerprints within three bits are guaranteed to share
a bucket — *d* differing bits touch at most *d* bands, so one band is
untouched. Past that, recall is probabilistic: at five bits roughly a quarter
of pairs are missed. That is affordable because groups are **single-linkage**,
and single linkage needs *k-1* of a group's *k(k-1)/2* pairs to hold together.
Measured on a planted corpus, a twelve-message monthly series with intra-group
distances up to eight came back as one group at ``d <= 5``. The knob to turn is
the distance, never the band width: eight-bit bands buy a guarantee out to
seven bits and a hundred and fifty-six million candidate pairs at a hundred
thousand messages, against three hundred thousand for sixteen.

Comparing one bucket of *k* distinct fingerprints costs ``k(k-1)/2`` Hamming
distances, and LSH bounds nothing: it promises that near-identical texts land
together, not that everything landing together is near-identical. So the pass
runs on a budget —
:attr:`~mailarc_analytics.derived.config.AnalyticsConfig.template_max_comparisons`,
spent smallest bucket first, with what it cannot afford counted rather than
quietly skipped. A budget and not a cap, because the expensive case and the
valuable case are opposites here: eight thousand *unrelated* fingerprints in
one band cost 6.5 seconds, the same eight thousand as *near-duplicates* cost
0.18 — every pair after the first unions is already in one component and skips
its distance — and a cap would throw the second away to bound the first.

Messages whose ``body_clean`` is empty hash to zero and are **discarded, not
grouped**. Everything empty is trivially alike, and one bucket holding every
quoted-only reply in the archive would otherwise be the highest-scoring finding
in it. The number is reported rather than swallowed.

The compute half is pure and needs no graph; the write half is two ``MERGE``
statements out of the catalogue.
"""

import logging
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import combinations, pairwise

from runic.ogm import Session

from mailarc_analytics.derived.config import AnalyticsConfig
from mailarc_analytics.derived.model import (
    InstanceOf,
    MessageFacts,
    Template,
    TemplateCluster,
    TemplateDirection,
    TemplateGroup,
    TemplateGrouping,
    TemplateMember,
    template_id,
)
from mailarc_analytics.derived.partition import DisjointSet
from mailarc_analytics.derived.writes import merge_rows
from mailarc_analytics.queries import catalog
from mailarc_core.archive.model import to_unsigned_64
from mailarc_core.mail.parsing import SIMHASH_BITS, hamming_distance

logger = logging.getLogger(__name__)

RUNAWAY_SHARE = 0.05
"""Share of a direction's messages one template may hold before it is flagged.

Single linkage can chain across an archive: A resembles B, B resembles C, and
nothing resembles both ends. ``d <= 5`` makes that unlikely rather than
impossible, and a template holding a twentieth of everything the user ever sent
is a calibration signal, not a finding. Warned about and kept, because throwing
it away would hide the calibration problem instead of showing it.
"""

RUNAWAY_MIN_MESSAGES = 100
"""Messages a direction needs before that share means anything.

A test corpus of thirty mails is *supposed* to be twelve monthly reports and
ten newsletters, and a warning on every such run is a warning nobody reads by
the time a real archive earns one.
"""


def band_keys(stored_simhash: int, band_bits: int) -> tuple[tuple[int, int], ...]:
    """The LSH buckets one fingerprint falls into, index included.

    :func:`~mailarc_core.archive.model.to_unsigned_64` runs first, always.
    Masking a negative value happens to give the right bits — Python shifts
    arithmetically over an infinite two's complement and the mask cuts the sign
    extension off again — but the same value then goes on to be hashed,
    rendered and compared, and there the sign is fatal. One conversion at the
    edge, and nothing after it has to know. The call is idempotent, so a caller
    handing in an already-converted value loses nothing.

    Bands are cut from the least significant end and any remainder at the top
    is simply not banded: with a band width that does not divide 64 the top
    bits stop contributing to candidate generation, which costs recall and
    nothing else. Sixteen divides it, which is why it is the default.
    """
    if band_bits < 1 or band_bits > SIMHASH_BITS:
        raise ValueError(f"band_bits must be between 1 and {SIMHASH_BITS}")
    value = to_unsigned_64(stored_simhash)
    mask = (1 << band_bits) - 1
    return tuple(
        (index, (value >> (index * band_bits)) & mask)
        for index in range(SIMHASH_BITS // band_bits)
    )


def group_templates(
    facts: Sequence[MessageFacts], config: AnalyticsConfig
) -> TemplateGrouping:
    """Cluster near-identical bodies, per direction. No I/O, no text, no graph.

    The first of A3's two halves, and it needs nothing but fingerprints — which
    is what lets a rebuild read a few hundred bodies afterwards instead of a
    hundred thousand up front. What comes back names its members; the words
    come later.
    """
    unhashable = sum(1 for one in facts if not one.simhash)
    groups: list[TemplateGroup] = []
    dropped = 0
    for direction in TemplateDirection:
        members = [one for one in facts if one.simhash and _direction(one) is direction]
        found, skipped = _cluster(members, direction, config)
        groups.extend(found)
        dropped += skipped
    if unhashable:
        logger.info(
            "%d messages have no body to fingerprint and cannot be templates",
            unhashable,
        )
    logger.info(
        "A3 found %d templates over %d messages (%d band buckets not compared)",
        len(groups),
        len(facts),
        dropped,
    )
    return TemplateGrouping(
        groups=tuple(groups),
        unhashable_messages=unhashable,
        dropped_buckets=dropped,
    )


def describe_templates(
    grouping: TemplateGrouping,
    known: Mapping[str, MessageFacts],
    bodies: Mapping[str, str],
    config: AnalyticsConfig,
) -> tuple[TemplateCluster, ...]:
    """Give each group its sample text and its score. Still pure.

    *bodies* is what :func:`~mailarc_analytics.derived.reader.read_bodies`
    returned for exactly these members; a message missing from it falls back to
    whatever ``body_clean`` the facts already carry, so a caller that read the
    text eagerly and one that read it late produce the same templates.
    """
    totals = _direction_totals(known)
    return tuple(
        _described(group, known, bodies, config, totals) for group in grouping.groups
    )


def write_templates(session: Session, clusters: Sequence[TemplateCluster]) -> None:
    """Write the templates and the ``INSTANCE_OF`` edge for each copy.

    Templates before their edges, because
    :data:`~mailarc_analytics.queries.catalog.MERGE_INSTANCE_OF` matches both
    endpoints instead of merging them. The edge carries the distance, so a
    reader can see whether a member is the text itself or the copy that drifted
    furthest from it.
    """
    merge_rows(
        session,
        catalog.MERGE_TEMPLATES,
        (
            {
                "id": template.id,
                "sample_text": template.sample_text,
                "occurrences": template.occurrences,
                "automation_score": template.automation_score,
                "direction": template.direction.value,
                "first_seen": template.first_seen,
                "last_seen": template.last_seen,
            }
            for template in clusters
        ),
        model=Template,
    )
    merge_rows(
        session,
        catalog.MERGE_INSTANCE_OF,
        (
            {
                "message_id": member.message_id,
                "template_id": template.id,
                "distance": member.distance,
            }
            for template in clusters
            for member in template.members
        ),
        model=InstanceOf,
    )
    logger.info("Wrote %d templates", len(clusters))


def automation_by_message(
    clusters: Sequence[TemplateCluster],
) -> dict[str, float]:
    """Every template's score, keyed by the messages that are instances of it.

    A3 answers in clusters and the importance scorer walks messages, so one of
    the two has to turn into the other — and it is this one, because the shape
    of a :class:`~mailarc_analytics.derived.model.TemplateCluster` belongs to
    this module and the scorer should no more know it than it knows how a
    fingerprint is banded.

    A message no template claimed is simply **absent**, not present with a
    zero. ``looks automated`` fires above a threshold, so the two would score
    the same either way — but "this is not a template" and "this is a template
    nobody could automate" are different findings, and a caller that wanted to
    tell them apart could not if this defaulted.

    A message that is somehow a member of two templates keeps the higher score,
    which cannot happen from a rebuild — the grouping partitions its messages —
    and is the reading that stays honest if a caller ever hands in two
    overlapping findings.
    """
    found: dict[str, float] = {}
    for template in clusters:
        for member in template.members:
            standing = found.get(member.message_id)
            if standing is None or template.automation_score > standing:
                found[member.message_id] = template.automation_score
    return found


def automation_score(
    sent_at: Sequence[datetime | None], body_words: int, config: AnalyticsConfig
) -> float:
    """How worth automating a template is, in ``0..1``.

    Frequency times regularity of the intervals times brevity, exactly as §6.3
    asks, with each factor in ``0..1``:

    ``frequency``
        ``log1p(occurrences) / log1p(frequency_saturation)``, capped at one.
        Saturating rather than linear because the spec's own comparison demands
        it — one monthly status mail must beat two hundred identical newsletter
        arrivals — and an unbounded term would hand the ranking to the mailing
        list.
    ``regularity``
        ``1 / (1 + coefficient of variation)`` over the gaps between sendings.
        Zero when there is nothing to judge, and zero when every gap is zero: a
        mass mailing that went out in one second is a burst, which is the
        opposite of a schedule.
    ``brevity``
        ``min(1, words / template_min_words) * ideal / (ideal + words)``. The
        second half is what separates a form from a letter; the floor is what
        stops the highest-scoring finding in every archive being the pile of
        one-word "Danke!" replies, which recur, recur regularly, and are as
        short as text gets. Measured, the floor moves twenty-eight of those
        from 0.995 to 0.040 and puts the monthly status report back on top.

    Multiplied and not averaged, so any one factor can veto: a text written
    twenty times at random moments is not a schedule, and a grunt sent daily is
    not work anybody wants back.

    *sent_at* carries one entry per member, ``None`` included, because the
    number of members is the frequency term while only the dated ones can say
    anything about regularity. The direction is deliberately absent — §6.3
    reports the two separately rather than scoring them differently, and a
    direction bonus folded in here would make the number incomparable across
    the two lists.

    Deterministic to the bit: sorting fixes the order the gaps are summed in,
    and the rounding keeps the value stable across a round trip through Cypher.
    """
    occurrences = len(sent_at)
    frequency = min(
        1.0, math.log1p(occurrences) / math.log1p(max(1, config.frequency_saturation))
    )
    regularity = _regularity(sorted(one for one in sent_at if one is not None))
    ideal = max(1, config.template_ideal_words)
    brevity = min(1.0, body_words / max(1, config.template_min_words)) * (
        ideal / (ideal + body_words)
    )
    return round(frequency * regularity * brevity, 6)


def _cluster(
    members: Sequence[MessageFacts],
    direction: TemplateDirection,
    config: AnalyticsConfig,
) -> tuple[list[TemplateGroup], int]:
    """Single-linkage clustering of one direction's fingerprints, and the
    number of band buckets the comparison budget could not afford.

    Identical fingerprints are joined first and outside the budget: they are at
    distance zero by definition, so five thousand byte-identical newsletters
    become one leader instead of twelve million comparisons — and they stay one
    template even in a bucket nothing is spent on.

    What remains is compared pairwise within each bucket, and a pair already in
    one component is skipped. That skip is why the budget is spent on
    comparisons rather than on bucket size: a genuine family joins up in its
    first few unions and every later pair costs a lookup, while a bucket of
    unrelated texts pays the full square. Smallest buckets first, so a
    four-message form is never the one that goes without, and charged up front
    so a bucket is compared whole or not at all. Sorted ascending means nothing
    after the first overflow can fit either, which is what ends the pass.
    """
    known = {one.id: one for one in members}
    partition = DisjointSet(known)
    by_fingerprint: dict[int, list[str]] = {}
    for message in members:
        by_fingerprint.setdefault(message.simhash, []).append(message.id)
    for ids in by_fingerprint.values():
        for left, right in pairwise(sorted(ids)):
            partition.union(left, right)

    leaders = {value: min(ids) for value, ids in by_fingerprint.items()}
    buckets: dict[tuple[int, int], set[int]] = {}
    for value in leaders:
        for key in band_keys(value, config.lsh_band_bits):
            buckets.setdefault(key, set()).add(value)

    pending = sorted(buckets.items(), key=lambda one: (len(one[1]), one[0]))
    budget = max(0, config.template_max_comparisons)
    dropped = 0
    for index, (_key, values) in enumerate(pending):
        comparisons = len(values) * (len(values) - 1) // 2
        if comparisons > budget:
            dropped = len(pending) - index
            logger.warning(
                "A3's comparison budget is spent; %d %s band buckets not compared",
                dropped,
                direction.value,
            )
            break
        budget -= comparisons
        for left, right in combinations(sorted(values), 2):
            if partition.find(leaders[left]) == partition.find(leaders[right]):
                continue
            if hamming_distance(left, right) <= config.simhash_max_distance:
                partition.union(leaders[left], leaders[right])

    found = [
        _group(component, known, direction)
        for component in partition.components()
        if len(component) >= config.template_min_occurrences
    ]
    return found, dropped


def _group(
    component: Sequence[str],
    known: Mapping[str, MessageFacts],
    direction: TemplateDirection,
) -> TemplateGroup:
    """One component as a group, measured against its own representative.

    The representative is the member with the smallest canonical id, which is
    what makes the key reproducible: membership is order-independent because a
    component is, and ``min`` over the members is order-independent too. Its
    fingerprint is the template's key and the zero every distance is from.
    """
    representative = min(component)
    fingerprint = known[representative].simhash
    return TemplateGroup(
        direction=direction,
        representative=representative,
        representative_simhash=fingerprint,
        members=tuple(
            TemplateMember(
                message_id=member,
                distance=hamming_distance(known[member].simhash, fingerprint),
            )
            for member in sorted(component)
        ),
    )


def _described(
    group: TemplateGroup,
    known: Mapping[str, MessageFacts],
    bodies: Mapping[str, str],
    config: AnalyticsConfig,
    totals: Mapping[TemplateDirection, int],
) -> TemplateCluster:
    """One group with the text read and the score computed."""
    sample = _shortest(group, known, bodies)
    dated = [
        known[member.message_id].sent_at if member.message_id in known else None
        for member in group.members
    ]
    occurrences = len(group.members)
    total = totals.get(group.direction, 0)
    if total >= RUNAWAY_MIN_MESSAGES and occurrences > total * RUNAWAY_SHARE:
        logger.warning(
            "Template %s holds %d of %d %s messages — check the distance threshold",
            group.representative,
            occurrences,
            total,
            group.direction.value,
        )
    return TemplateCluster(
        id=template_id(group.representative_simhash, group.direction),
        direction=group.direction,
        sample_text=sample[: config.template_sample_length],
        automation_score=automation_score(dated, len(sample.split()), config),
        first_seen=min((one for one in dated if one is not None), default=None),
        last_seen=max((one for one in dated if one is not None), default=None),
        members=group.members,
    )


def _shortest(
    group: TemplateGroup,
    known: Mapping[str, MessageFacts],
    bodies: Mapping[str, str],
) -> str:
    """The shortest body in the group — the sample and the word count.

    Shortest rather than first, because it is the copy with the least filled
    in: the month name, the invoice number and the customer's name are what
    make a template's instances differ, and the smallest one shows the form
    with the fewest of them. Ties go to the smaller id so a rebuild picks the
    same text twice.

    A group whose bodies are all missing answers with the empty string, which
    scores zero through the brevity floor rather than raising. That is only
    reachable when the graph lost the text between two reads, and losing the
    sample is a better outcome than losing the rebuild.
    """
    texts = [
        (len(text), member.message_id, text)
        for member in group.members
        for text in (_body(member.message_id, known, bodies),)
        if text
    ]
    return min(texts, default=(0, "", ""))[2]


def _body(
    message_id: str, known: Mapping[str, MessageFacts], bodies: Mapping[str, str]
) -> str:
    """This message's cleaned body, from the late read or from the facts."""
    late = bodies.get(message_id)
    if late:
        return late
    fact = known.get(message_id)
    return fact.body_clean if fact is not None else ""


def _direction_totals(
    known: Mapping[str, MessageFacts],
) -> dict[TemplateDirection, int]:
    """How many messages went each way — the denominator of the runaway check."""
    totals = dict.fromkeys(TemplateDirection, 0)
    for message in known.values():
        totals[_direction(message)] += 1
    return totals


def _direction(message: MessageFacts) -> TemplateDirection:
    """Which way this message went, as far as the account list can tell.

    ``outbound`` is the reader's comparison of the sender against the archive's
    own ``Account`` addresses, so a shared mailbox or an alias nobody imported
    makes the user's own mail read as received. That is a gap in the account
    list rather than in this analysis, and it is worth knowing before the first
    bug report.
    """
    return TemplateDirection.SENT if message.outbound else TemplateDirection.RECEIVED


def _regularity(times: Sequence[datetime]) -> float:
    """``1 / (1 + coefficient of variation)`` over the gaps between sendings.

    Zero below three dated members, because two of them make one gap and one
    gap has no variation to measure — it would score a perfect schedule out of
    a coincidence. Zero again when the mean gap is zero: everything sent in the
    same second is a mass mailing, which is the opposite of the monthly report
    this factor is looking for.
    """
    if len(times) < 3:
        return 0.0
    gaps = [(later - earlier).total_seconds() for earlier, later in pairwise(times)]
    mean = sum(gaps) / len(gaps)
    if mean <= 0.0:
        return 0.0
    deviation = math.sqrt(sum((gap - mean) ** 2 for gap in gaps) / len(gaps))
    return 1.0 / (1.0 + deviation / mean)
