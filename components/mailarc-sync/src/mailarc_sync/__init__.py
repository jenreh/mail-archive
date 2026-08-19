"""Import orchestration: what to fetch next, and what a crash may not lose.

The engine drives a mail source through one account's mailbox and hands every
message to :mod:`mailarc_core`'s archive writer; the job queue is the part that
outlives a restart, so progress is a row rather than a stack frame. Provider
*implementations* live elsewhere — this package knows the port, never Gmail.

Empty until phase 2 fills ``engine/`` and ``jobs/``.
"""
