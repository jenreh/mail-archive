"""The one component allowed to see Reflex.

Pages and routes stay in ``app/``; what belongs here are the states and the
components behind them, so the application layer is left with composition and
wiring. Grouped by what the user is doing — connecting an account, importing,
reviewing what was imported, checking what was derived from it — rather than by
Reflex's own vocabulary.
"""
