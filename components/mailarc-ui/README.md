# mailarc-ui

The only component allowed to see Reflex.

**The whole interface lives here — pages included.** `app/` keeps the
composition root, the configuration and the entry points, and imports a page
module for the single purpose of letting its decorator register the route. The
packages are named after what the user is doing, not after Reflex's own
vocabulary:

```text
accounts/   connecting a mailbox and running its consent flow.
imports/    starting an import and following the job row it queued.
search/     the front door — the form, the result list, the message.
message_detail/
            one open message, as a state mixin and a pane that takes the
            page's own state class.
insights/   finding a message, what a rebuild derived, and whether the
            co-addressed edge still agrees with the archive it came from.
embedder/   configuring the embedder and running a rebuild of the vectors.
dashboard/  the welcome page's six panels, and the line across the middle
            of them — half of it is public, half of it is administration.
status/     the live state of the graph server this archive is built on.
```

Three packages cut across those, because the design has exactly one of each:

```text
kit/        the four primitives every page is drawn with — panel_card,
            card_heading, stat_tile, page_header — plus the page frame's
            two spacing constants.
shell/      routes.py (every path, named once) · model.py · navigation.py
            (the sidebar, from data) · templates.py (the two layouts and
            the decorator a public page uses) · access.py (the one gate).
pages/      one module per route and no logic at all.
styles.py   base_style / base_stylesheets, including the archive's own
            `assets/css/mail-archive.css`.
```

`assets/` stays at the repository root — Reflex serves it from there — so a
`--ma-*` token is declared outside this component and read from inside it.

The search panel in `insights/` is the one place where "nothing configured"
must not look like "nothing found". `app_semantic_provider` defaults to
`none`, so a fresh installation has no embedder: the semantic half then says
so in the sentence that names the setting, and the full-text half goes on
working. An empty result list would read as a claim about the archive.

## Rules

- May import `mailarc-core`, `mailarc-sync`, `mailarc-analytics`, Reflex and
  the appkit UI packages (`appkit-mantine`, `appkit-ui`, `appkit-user`). It is
  expressly exempt from the no-Reflex rule that binds every other component —
  see `test_isolation.py`.
- **Never imports `app`.** Configuration and construction arrive from
  `app/composition.py`; a state asks, it does not build.
- **No `runic.rag`.** The exemption is Reflex, nothing else.
