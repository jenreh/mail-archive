# Insights

**Insights** in the rail (`/insights`) shows what an analysis made of the
archive. The search page at `/` shows what the import wrote. This page shows
what was worked out from it: who is written to together, which mail belongs to
one piece of work, which of it probably matters, and what you have filed under
a name of your own.

Nothing on this page is written by a language model. Every number is arithmetic
over the headers, and every grouping can be recomputed from the same messages
with the same result.

## Nothing is derived until you ask

**Rebuild** queues the work as a job, and the bar under it climbs through ten
stages while the page stays usable. **Cancel** stops it at the next stage
boundary.

A rebuild throws the whole derived layer away and computes it again. Running it
twice over an unchanged archive changes nothing, and a cancelled run costs only
the stages it had reached.

| Stage | What it does |
| --- | --- |
| `delete` | Removes every derived node and edge, and clears the four scored properties |
| `read` | Pages the messages, their reply parents and their signals out of the graph |
| `correspondents` | Counts who is addressed together, and which sets of people recur |
| `centrality` | Ranks addresses by how central they are among their correspondents |
| `communities` | Partitions the correspondents into circles |
| `topics` | Clusters messages that belong to the same piece of work |
| `keywords` | Picks the words that tell one topic from the others |
| `templates` | Finds texts written again and again with barely a word changed |
| `importance` | Scores each message, and records why |
| `suggestions` | Offers each tag the untagged mail that looks like it belongs |

The rebuild is a full pass every time. There is no incremental version of it,
so the honest way to read any finding is "as of the last rebuild".

## The cross-check comes first

The panel at the top is the only one that can say something is *wrong* rather
than merely say what was found. Who is addressed together is the one finding
that can be worked out two ways, from the stored edge a rebuild wrote and from
the messages themselves, counted again from scratch. The page asks both and
holds the answers against each other.

- **Teal** means the two agree on every pair the check could rule on.
- **Yellow** means the messages count more than the edge does. That is usually
  harmless, e.g. no rebuild has run since the last import.
- **Red** means the edge claims more than the messages support, or names a pair
  the messages never produced. Nothing legitimate does that, so a red verdict
  means the number is wrong rather than stale.

## Topics

A topic is a set of messages that one of six signals says belong together, plus
a seventh on an installation with an embedder configured. The **Signal** badge
names the one that drew the cluster, and the difference is the whole reason the
column exists. A `ref` topic is two messages naming the same ticket, which is a
fact read out of a header. An `embedding` topic is two messages a model thinks
are about the same thing, which is a guess. A cool badge is a signal that
carries a topic on its own, and a warm one carries a topic only together with
another signal.

A message can appear under more than one topic. One row is one topic and one
signal, so two readings of the same mail are never added up.

The **About** column holds the topic's keywords. They are the words that occur
often in this topic and rarely in the others, worked out by counting terms
across the whole set of topics. Eight words per topic, read from at most twenty
of its messages and the first 2000 characters of each. That ceiling is why a
topic of five hundred messages costs the same as a topic of twenty.

Common words, ticket references and bare numbers are dropped before the
counting, so what is left is what a person would use to tell the topics apart.

## Circles

A circle is a group of correspondents who write to the same people. It comes
out of label propagation over the whole co-addressing graph, which means two
members of a circle need never have shared a single message. That is the
difference between a circle and a **group**: a group is one exact set of people
some message was addressed to, and a circle is a partition of everybody.

The name of a circle is the commonest domain among its members. Nobody invents
it. Where two domains are equally common, the name goes to the one the
best-ranked member uses.

A circle needs at least three members. The listing is ordered by how much mail
circulates in the circle rather than by how many people are in it, because a
circle of forty who exchanged three mails is a directory rather than a working
group. A message counts as circulating in a circle when at least half of its
participants are members.

Circles are recomputed by every rebuild. See [the graph
explorer](./graph-explorer.md) for what that means for a link.

## What probably matters

Every message gets a score between 0 and 1, and every score names the terms
that produced it. The **Why** column is the point of the whole card. The reason
importance is arithmetic rather than a judgement from a model is that you can
argue with arithmetic, and a bar with its terms hidden behind a hover would be
a ranking nobody can correct.

| Reason | Worth | Fires when |
| --- | --- | --- |
| `replied by you` | +0.30 | You answered the message |
| `flagged by the provider` | +0.25 | The message wears a label named `IMPORTANT` or `STARRED` |
| `3 replies` | +0.20 | Somebody answered. Full value at three answers |
| `sent by a central correspondent` | +0.15 | The sender ranks at half the archive's top rank or better |
| `addressed directly` | +0.15 | You were on the To line rather than the Cc line |
| `few recipients` | +0.10 | The To line named three addresses or fewer |
| `has attachments` | +0.10 | The message carries a file |
| `looks automated` | -0.40 | The text belongs to a template that is sent on a schedule |

The positive terms add up to more than 1, so a message carrying every one of
them is clamped rather than scaled. That keeps one archive's numbers comparable
with another's.

Two honest limits are worth knowing.

**`flagged by the provider` is a Gmail-only reason today.** Gmail passes its own
label names through unchanged, so `IMPORTANT` and `STARRED` reach the graph as
labels. IMAP's `\Flagged` and Microsoft 365's flags are not imported at all, so
on those mailboxes the reason never fires.

**Nothing here has read a word of the body.** A message nobody answered can
still be the one that mattered. This is a ranking aid rather than a verdict.

The scores are written by a rebuild and cleared by the next one. A message that
has never been through a rebuild has no score, which is not the same as a score
of zero, and the page says "nothing has been scored yet" rather than showing an
empty ranking.

## Tags

A tag is the one grouping on this page that is a decision rather than a
finding. Topics and circles are recomputed and thrown away by every rebuild. A
tag is what you meant, and it survives every rebuild and every re-import.

You make a tag by promoting a cluster. Pick a topic or a circle in [the graph
explorer](./graph-explorer.md), type a name for it, and every message in the
cluster wears the tag from then on. You can also put a tag on one message at a
time, from the reading pane in the explorer's details column.

The tags card lists what exists, how much mail each holds, and two verbs per
row.

- **Accept N suggested** puts the tag on the mail it is being offered, in one
  write and up to fifty messages at a time.
- **Delete** removes the tag and every membership on it. The messages
  themselves are untouched.

Where a tag is being offered nothing, the row says so in words instead of
showing a zero. A disabled button beside a nought reads as a broken analysis,
and the honest reading is usually that no rebuild has run since the tag was
made.

A tag can end up holding nothing. Deleting a mailbox removes the messages it
was the only holder of, and their memberships go with them, so the tag stays
with a count of zero. That is a tag whose mail is gone rather than a fault, and
it is listed so you can delete it on purpose.

Renaming a tag does not move it. The key comes from the name you first gave it
and every membership points at that key, so a rename changes what is displayed
and nothing else.

### Suggestions

After each rebuild, every tag is offered the untagged mail that looks like it
belongs. A suggestion is made when a group of messages already holds at least two
members wearing the tag, and those members are at least 30 % of the group.
Three kinds of group can argue for one.

| Group | Score | Why it is worth that |
| --- | --- | --- |
| Thread | 0.9 | The mail itself says these messages answer each other |
| Topic | 0.7 | One of the topic signals above put them together |
| Circle | 0.5 | These people write to the same people |

The score of a suggestion is the weight of the best group arguing for it, times
the share of that group already wearing the tag. The best single argument wins,
so two weak arguments cannot outvote one strong one.

Both guards exist to keep the offers quiet. One tagged message in a group is a
coincidence, and would turn the first tag on a busy thread into fifty
suggestions. Two tagged messages out of two hundred is a mailing list somebody
filed twice.

**Suggestions live and die with a rebuild.** They are derived, so every rebuild
deletes them and works them out again. A tag you made an hour ago is offered
nothing at all until another rebuild runs, and the card says so and puts the
rebuild button next to the sentence.

### Accepting suggestions automatically

Off by default, and it is the one setting here that is a decision rather than a
threshold. A tag is a person's word for a set of messages, and the whole
argument for the annotation layer is that nothing writes to it except a person.

```yaml
app:
  analytics:
    tag_auto_accept: true
    tag_auto_accept_min_score: 0.6
```

With it on, the worker takes every suggestion at or above the score after each
rebuild and applies it. The threshold sits above what the weakest kind of group
can produce, so however much of a circle already wears a tag, a circle on its
own never tags another message. A thread or a topic can.

Every membership written this way is marked `auto` rather than `accepted`, so
what the analysis did stays visible afterwards. A message that already wears
the tag is left alone, keeping the date and the source of the earlier decision.

See the analytics section of [Configuration](./configuration.md) for the rest
of the settings behind these findings.

## Templates and groups

Two more findings sit on the same page. **Groups** are the exact sets of people
who get written to together repeatedly. **Templates** are texts written again
and again with barely a word changed, split into what you send and what you
receive, because only the mail you write yourself can be automated.

## Every listing links into the graph

Each row has a **Graph** pill that opens [the explorer](./graph-explorer.md)
rooted at that row. Two of the four kinds of link go stale by design. A topic id
and a circle id are digests of their members, so the next rebuild mints new
ones, and a page left open across a rebuild sends you to a cluster that no
longer exists. The explorer answers that with a sentence rather than a blank
canvas.

A message id and a tag id keep. If you want a reference that survives, promote
the cluster to a tag.

## What this page does not gate

The listings read every mailbox of the installation at once. A co-recipient
listing says who writes to whom across all of them, and there is no sign-in and
no per-mailbox permission. The boundary is whoever can open the window.
