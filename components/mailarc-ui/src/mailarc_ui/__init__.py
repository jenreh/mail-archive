"""The one component allowed to see Reflex.

The whole user interface: the pages, the shell they sit in, the states behind
them and the components those render. ``app/`` keeps the composition root, the
configuration and the entry points, and imports a page module for the single
purpose of letting its decorator register the route.

Grouped by what the user is doing — connecting an account, importing, searching
what was imported, checking what was derived from it, seeing how the graph
server is doing — rather than by Reflex's own vocabulary. Three packages cut
across that: ``kit`` holds the four primitives every page is drawn with,
``shell`` holds the navigation and the layouts, and ``pages`` holds one module
per route and no logic at all.

Nothing here imports ``app``. Everything the composition root builds arrives
through the service registry, looked up inside the method that needs it.
"""
