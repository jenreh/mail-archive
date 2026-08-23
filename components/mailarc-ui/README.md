# mailarc-ui

The only component allowed to see Reflex.

Pages, routes and composition stay in `app/`; what lives here are the states
and the components behind them, so the application layer keeps to wiring. The
packages are named after what the user is doing, not after Reflex's own
vocabulary:

```text
accounts/   connecting a mailbox and running its consent flow.
imports/    starting an import and following the job row it queued.
review/     reading the archive — a two-pane list and message reader.
insights/   finding a message, what a rebuild derived, and whether the
            co-addressed edge still agrees with the archive it came from.
```

The search panel in `insights/` is the one place where "nothing configured"
must not look like "nothing found". `app_semantic_provider` defaults to
`none`, so a fresh installation has no embedder: the semantic half then says
so in the sentence that names the setting, and the full-text half goes on
working. An empty result list would read as a claim about the archive.

## Rules

- May import `mailarc-core`, `mailarc-sync`, `mailarc-analytics`, Reflex and
  the appkit UI packages. It is expressly exempt from the no-Reflex rule that
  binds every other component — see `test_isolation.py`.
- **Never imports `app`.** Configuration and construction arrive from
  `app/composition.py`; a state asks, it does not build.
- **No `runic.rag`.** The exemption is Reflex, nothing else.
