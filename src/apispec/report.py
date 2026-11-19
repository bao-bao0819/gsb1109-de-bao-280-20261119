"""Generic aggregation of diagnostics produced by plugins."""

from __future__ import annotations

import typing


class AggregateConversionReport:
    """Combine several plugin conversion reports into one view.

    The aggregate is read-only.  It exposes the same interface as the reports
    it combines: iteration, ``len``, ``issues``, ``occurrences``,
    ``references``, ``for_operation`` and ``to_dict``.
    """

    def __init__(self, reports: typing.Sequence[typing.Any]) -> None:
        self.reports = list(reports)

    @property
    def issues(self) -> list[typing.Any]:
        issues = []
        for report in self.reports:
            for issue in getattr(report, "issues", []):
                if issue not in issues:
                    issues.append(issue)
        return issues

    @property
    def occurrences(self) -> list[typing.Any]:
        return [
            occurrence
            for report in self.reports
            for occurrence in getattr(report, "occurrences", [])
        ]

    @property
    def entries(self) -> list[typing.Any]:
        return self.occurrences

    @property
    def references(self) -> list[typing.Any]:
        return [
            reference
            for report in self.reports
            for reference in getattr(report, "references", [])
        ]

    def __iter__(self) -> typing.Iterator[typing.Any]:
        return iter(self.occurrences)

    def __len__(self) -> int:
        return sum(len(report) for report in self.reports)

    def __bool__(self) -> bool:
        return bool(len(self))

    def __getitem__(self, index):
        return self.occurrences[index]

    def for_operation(self, path=None, method=None):
        occurrences = []
        for report in self.reports:
            for_operation = getattr(report, "for_operation", None)
            if for_operation is None:
                continue
            occurrences.extend(for_operation(path, method))
        return occurrences

    def to_dict(self) -> list[dict[str, typing.Any]]:
        return [
            entry
            for report in self.reports
            for entry in getattr(report, "to_dict", lambda: [])()
        ]

    def log(self) -> None:
        for report in self.reports:
            log = getattr(report, "log", None)
            if log is not None:
                log()

    def __repr__(self) -> str:
        return (
            f"<AggregateConversionReport reports={len(self.reports)} "
            f"entries={len(self)}>"
        )
