"""Finding a message in the archive — the page a person arrives at.

``model``
    The form's strings on the way in and a result row on the way out, plus the
    formatting between them: initials, a relative time, a relevance. Knows no
    I/O.
``reads``
    The two services the page reads through, taken out of the service registry
    inside the function that needs one, and the account picker's options.
``state``
    ``MailSearchState`` — the filled form, the page of results and, through
    ``mailarc_ui.message_detail``'s mixin, the message a row opens.
``form``
    The left column: the question, the path, and the fields that narrow it.
``components``
    The result list and the three-column panel a page drops in.

The reading pane is not here. It is :mod:`mailarc_ui.message_detail`, which
takes a state class rather than naming one, and is handed this page's so the
open message belongs to this page.
"""

from mailarc_ui.search.components import LIST_WIDTH, result_list, search_panel
from mailarc_ui.search.form import FORM_WIDTH, search_form
from mailarc_ui.search.model import (
    ATTACH_ANY,
    ATTACH_WITH,
    ATTACH_WITHOUT,
    MODE_FULLTEXT,
    MODE_SEMANTIC,
    SEARCH_FAILED,
    SEMANTIC_IS_TEXT_ONLY,
    ResultRow,
    SearchAnswer,
    filters_of,
    initials_of,
    parse_date,
    percent_label,
    relative_label,
)
from mailarc_ui.search.state import PAGE_SIZE, SEMANTIC_HITS, MailSearchState

__all__ = [
    "ATTACH_ANY",
    "ATTACH_WITH",
    "ATTACH_WITHOUT",
    "FORM_WIDTH",
    "LIST_WIDTH",
    "MODE_FULLTEXT",
    "MODE_SEMANTIC",
    "PAGE_SIZE",
    "SEARCH_FAILED",
    "SEMANTIC_HITS",
    "SEMANTIC_IS_TEXT_ONLY",
    "MailSearchState",
    "ResultRow",
    "SearchAnswer",
    "filters_of",
    "initials_of",
    "parse_date",
    "percent_label",
    "relative_label",
    "result_list",
    "search_form",
    "search_panel",
]
