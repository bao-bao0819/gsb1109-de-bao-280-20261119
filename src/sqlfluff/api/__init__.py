"""Elements which wrap the sqlfluff core library for public use."""

# Expose the simple api
from sqlfluff.api.info import list_dialects, list_rules
from sqlfluff.api.review import (
    Diagnostic,
    EditSuggestion,
    FieldOccurrence,
    FieldReviewGroup,
    HintRecord,
    ReviewReport,
    build_review_report,
    review_report_as_dict,
)
from sqlfluff.api.simple import APIParsingError, fix, lint, parse

__all__ = (
    "lint",
    "fix",
    "parse",
    "APIParsingError",
    "list_rules",
    "list_dialects",
    "HintRecord",
    "FieldOccurrence",
    "EditSuggestion",
    "Diagnostic",
    "FieldReviewGroup",
    "ReviewReport",
    "build_review_report",
    "review_report_as_dict",
)
