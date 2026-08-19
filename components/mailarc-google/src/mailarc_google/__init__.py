"""Gmail, behind the one seam this design allows to be abstract.

Implements the core's mail source port and nothing else — OAuth, paging, and
the mapping from Google's JSON to the domain's value objects — so that provider
shapes stop at this boundary. It hangs off the core alone: the engine that
drives it never learns its name, because ``app/composition.py`` does the
registering.

Empty until phase 3 fills ``source/``.
"""
