# The graph explorer

**Graph** in the rail (`/graph`) draws one corner of the archive. [Insights](./insights.md)
answers the same questions as tables, and this page answers them as a picture:
who is around a message, which people a topic runs through, how two
correspondents are connected.

It never draws the whole archive. Every view is rooted at one thing and every
read behind it stops at 100 rows, because a canvas holding a hundred thousand
nodes is a grey rectangle.

## The three columns

**Left** is what to draw and how to draw it. **Middle** is the drawing.
**Right** is whatever you picked. The dividers between them can be dragged, so
you can take room from either side for the canvas.

## Choosing what to draw

Pick a **View** first. Five of the six need something to root the picture at,
which you then choose in the **Rooted at** box below. That box is searchable
and lists what the archive actually holds.

| View | `?view=` | Draws |
| --- | --- | --- |
| Map | `overview` | Every topic, circle and tag, and where they overlap. No mail at all |
| Topic | `topic` | A topic's messages and the people on them |
| Circle | `community` | A circle's members and the mail that circulates in it |
| Tag | `tag` | The mail wearing one tag |
| Person | `address` | One correspondent, the people they are written to alongside, and their mail |
| Mail | `message` | One message with its people, its thread, its labels, its files and the answers around it |

**Map** is the one view with no root. It shows the collections and how much mail
two of them share, which is the fastest way to see whether a topic and a circle
are the same piece of work under two names.

**Reply depth** appears in the Mail view only. It decides how far the picture
follows the answers around the message, from one hop to three.

## Choosing how to draw it

**Size by** picks the number a node's diameter comes from.

| Size by | Means |
| --- | --- |
| Nothing | Every node the same. The honest default before a rebuild has scored anything |
| Connections here | How many of the drawn edges touch this node |
| Reply centrality | How central a message is in the archive's reply chains |
| Importance | The score from [What probably matters](./insights.md#what-probably-matters) |
| How much it holds | The mail a collection holds, or how often a pair is written to together |

Two things about sizing are worth knowing.

**The scale is the picture, not the archive.** The heaviest node in this view is
drawn at the largest size and everything else is a fraction of it. An importance
of 0.4 means "middling" across an archive and "the smaller of these two" in a
view of two messages, and a drawing can only show the second reading.

**A node with no such number is drawn in the middle**, not at the smallest size.
A thread has no importance and an address has no reply rank. The smallest circle
would be a claim that the archive has not made.

**Arrangement** picks the layout. **Force** spreads the nodes apart by simulated
forces and reads well when you know nothing about the graph yet. **Concentric**
puts the heaviest node in the centre, which suits a view rooted at one thing.
**Tree** lays the nodes out in ranks, which suits a reply chain where the
direction is the story.

**Show** is both the legend and a set of switches. Each of the six kinds has a
dot in its own colour, and pressing one takes that kind off the canvas. Hiding a
kind hides its edges too, so hiding the addresses in a topic view leaves the
messages standing alone rather than leaving arrows pointing at nothing.

**Fit** puts the whole picture back inside the frame. Pressing it again on a
picture that is already fitted fits it again.

## Working on the canvas

- **Tap a node** to select it. The details column on the right fills in.
- **Double-tap a node** to expand it. One hop out of that node is fetched and
  laid over what is already drawn, so the picture grows rather than being
  replaced.
- **Tap the background** to clear the selection.
- Scroll to zoom and drag to pan.

Changing the size, the arrangement or the visible kinds redraws what is already
on screen and asks the archive nothing. Only changing the view, the root, the
depth or expanding a node reads again.

## What the details column shows

The right column answers in the terms the picked node deserves.

- **A message** opens the reading pane, the same two tabs the search page uses,
  with the tags it wears above them. You can add or remove a tag here.
- **A topic or a circle** shows what the store holds about it, a form that turns
  it into a tag, and the members that are drawn on the canvas. The table lists
  what is in the picture rather than the whole cluster, because the picture is
  capped. Promoting takes the whole cluster.
- **A tag** shows what it holds and what the analysis is offering it, with a
  way to accept one suggestion or all of them.
- **An address** offers **Route from the root**, which redraws the canvas as the
  shortest connections between that person and the root of the current view.

The route is over co-addressing and in both directions, so a hop means "these
two were written to together". It looks up to four hops out and draws the three
shortest routes it finds, because the second route is often the interesting one.
A path through the messages themselves would alternate person, mail, person and
report twice the hop count anybody would call it.

## Links into this page go stale, and tags do not

Every listing on the insights page has a **Graph** pill that opens this page
rooted at that row. The same view is a plain URL, e.g.
`/graph?view=topic&id=topic:8f1c...`, so you can bookmark one or paste it to
somebody else.

**Two of the four kinds of link stop working after the next rebuild.** A topic
id and a circle id are digests of their own members. A rebuild computes the
clusters again from scratch and mints new ids, so a link written down yesterday
names a cluster that no longer exists, even when the same messages are still
grouped the same way.

When that happens the page says so:

> This topic was recomputed — pick it again. A cluster's id is a digest of its
> members, so every rebuild mints a new one; a tag is the reference that
> survives.

It says that rather than showing an empty canvas, because an empty canvas would
claim the topic holds nothing, and that claim would be false.

**A tag is the reference that keeps.** A message id keeps as well, because a
message's identity comes from the message. If you want to come back to a
cluster tomorrow, promote it to a tag with the form in the details column. The
tag records that it came from a topic or a circle, and never records the id it
came from, so nothing about it can go stale.

## When the canvas is empty

| What it says | What it means |
| --- | --- |
| Pick something to look at | The view needs a root and you have not chosen one |
| Nothing to draw yet | No rebuild has run, so there are no topics, circles or scores to draw |
| This topic was recomputed | The id in the link belongs to a cluster a later rebuild replaced |
| The archive has nothing to draw for that | The root exists and holds nothing, e.g. a tag whose mail was deleted |

When a read hits the 100 row ceiling, a notice above the canvas names what was
cut, so a partial picture never looks like a complete one.

## What it costs

The graph library is about 400 KB and loads only when you open this page. Every
other page in the application pays nothing for it.
