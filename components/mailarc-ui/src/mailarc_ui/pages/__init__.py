"""Every page of the archive, one module each.

Deliberately empty of imports. A page module registers its route with Reflex as
a side effect of being imported, so an ``__init__`` that pulled all of them in
would make ``from mailarc_ui.pages import accounts`` register seven pages — and
a test that wanted to look at one of them would be looking at an application it
never started. ``app/app.py`` names each module it wants, and that list is the
application's page table.

What a page module holds is a route, a title, a gate and a layout. The body is
a component from the package that owns it — ``accounts_panel``,
``review_panel``, ``insights_panel``, ``embedder_panel``, ``status_panel`` —
and never logic of its own. A page that starts computing something is a panel
that has not been written yet.
"""
