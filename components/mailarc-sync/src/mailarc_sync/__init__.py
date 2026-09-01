"""Import orchestration: what to fetch next, and what a crash may not lose.

The engine drives a mail source through one account's mailbox and hands every
message to :mod:`mailarc_core`'s archive writer; the job queue is the part that
outlives a restart, so progress is a row rather than a stack frame. Provider
*implementations* live elsewhere — this package knows the port, never Gmail.

``erase/`` is the same pair of stores read the other way round: clearing a
mailbox has to undo the graph writes *and* the ledgers that say what has
already been fetched, and this is the only package that may know both.
"""
