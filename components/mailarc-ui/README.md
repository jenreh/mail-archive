# mailarc-ui

The only component allowed to see Reflex.

Pages, routes and composition stay in `app/`; what lives here are the states
and the components behind them, so the application layer keeps to wiring. The
packages are named after what the user is doing — `accounts/`, `imports/`,
`insights/` — not after Reflex's own vocabulary.

Empty until phase 4 fills those three.

## Rules

- May import `mailarc-core`, `mailarc-sync`, `mailarc-analytics`, Reflex and
  the appkit UI packages. It is expressly exempt from the no-Reflex rule that
  binds every other component — see `test_isolation.py`.
- **Never imports `app`.** Configuration and construction arrive from
  `app/composition.py`; a state asks, it does not build.
- **No `runic.rag`.** The exemption is Reflex, nothing else.
