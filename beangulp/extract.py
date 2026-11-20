__copyright__ = "Copyright (C) 2016-2017  Martin Blais"
__license__ = "GNU GPLv2"

import io
import bisect
import datetime
import heapq
import operator
import textwrap
import warnings

from typing import Callable
from typing import NamedTuple
from typing import TYPE_CHECKING
from typing import Tuple
from typing import List

from beancount.core import data
from beancount.parser import printer
from beangulp import similar

if TYPE_CHECKING:
    from beangulp.importer import Importer

# Header for the file where the extracted entries are written.
HEADER = ";; -*- mode: beancount -*-\n"

# Format for the section titles separating entries extracted from
# different documents. This is used as a format sting passing the
# document filesystem path as argument.
SECTION = "**** {}"

# Format for the title of a section reporting ordering relations that
# cannot be satisfied. This is used as a format string passing the
# account and date the conflicting relations are scoped to.
CONFLICT_SECTION = ";; Conflicting ordering relations for {} on {}:"

# Metadata field that indicates the entry is a likely duplicate.
DUPLICATE = "__duplicate__"

ExtractedEntry = Tuple[str, data.Entries, data.Account, "Importer"]

# A reference to an entry extracted from a document, composed of the
# document filesystem path and the line number of the entry in the
# document, as recorded in the "lineno" metadata field.
EntryRef = Tuple[str, int]

# A declared ordering relation between two entries extracted for the
# same account on the same date, as (account, date, before, after)
# where before and after are EntryRef tuples. The entry identified by
# before must appear in the merged output before the entry identified
# by after. Relations are only valid within the given account and
# date: they are ignored when the referenced entries were extracted
# for a different account or carry a different date.
OrderingRelation = Tuple[str, datetime.date, EntryRef, EntryRef]

# An entry in the merged output stream, as (entry, filepath, account)
# where filepath is the filesystem path of the document the entry was
# extracted from and account is the account associated to the document
# by the importer.
MergedEntry = Tuple[data.Directive, str, data.Account]


class MergeConflict(NamedTuple):
    """Ordering relations that cannot all be satisfied.

    Attributes:
      account: The account the conflicting relations are scoped to.
      date: The date the conflicting relations are scoped to.
      relations: The ordering relations contradicting each other.
      entries: The entries that could not be ordered and are therefore
        omitted from the merged output.
    """

    account: data.Account
    date: datetime.date
    relations: List[OrderingRelation]
    entries: List[MergedEntry]


def extract_from_file(
    importer: "Importer", filename: str, existing_entries: "data.Directives"
) -> data.Entries:
    """Import entries from a document.

    Args:
      importer: The importer instance to handle the document.
      filename: Filesystem path to the document.
      existing_entries: Existing entries.

    Returns:
      The list of imported entries.
    """
    entries = importer.extract(filename, existing_entries)
    if not entries:
        return []

    # Sort the newly imported entries.
    importer.sort(entries)

    # Ensure that the entries are typed correctly.
    for entry in entries:
        data.sanity_check_types(entry)

    return entries


def sort_extracted_entries(extracted: List[ExtractedEntry]) -> None:
    """Sort the extraxted entries.

    Sort extracted entries, grouped by source document, in the order
    in which they will be used in deduplication and in which they will
    be serialized to file.

    Args:
      extracted: List of (filepath, entries, account, importer) tuples
        where entries is the list of entries extracted from the
        document at filepath by importer.

    """

    # The entries are sorted on a key composed by (max-date, account,
    # min-date, filename) where max-date and min-date are the latest
    # and earliest date appearing in the entries list.  This should
    # place entries from documents produced earlier in time at before
    # ones coming from documents produced later.
    #
    # Most imports have a balance statement at the end with a date
    # that is one day later than the reporting period (balance
    # statement are effective at the beginning of the day).  Thus
    # using the end date should be more predictable than sorting on
    # the earliest entry.
    #
    # This diagram, where the bars represents the time span covered by
    # contained entries, represent the sort order we want to obtain:
    #
    # Assets:Ali   (-----)
    # Assets:Ali   (=====--------)
    # Assets:Bob    (------------)
    # Assets:Bob             (===----)
    # Assets:Ali                 (--------------)
    # Assets:Bob                (====-----------)
    #
    # The sections marked with = represent the time spans in which
    # duplicated entries could be present.  We want entries form
    # documents produced earlier in time to take precedence over
    # entries from documents produced later in time.

    def key(element: ExtractedEntry):
        filename, entries, account, importer = element
        dates = [entry.date for entry in entries]
        # Sort documents that do not contain any entry last.
        max_date = min_date = datetime.date(9999, 1, 1)
        if dates:
            max_date, min_date = max(dates), min(dates)
        return max_date, account, min_date, filename

    extracted.sort(key=key)


def find_duplicate_entries(
    extracted: List[ExtractedEntry], existing: data.Entries
) -> List[ExtractedEntry]:
    """Flag potentially duplicate entries.

    Args:
      extracted: List of (filepath, entries, account, importer) tuples
        where entries is the list of entries extracted from the
        document at filepath by importer.
      existing: Existing entries.

    Returns:
      A copy of the list of new entries with the potentially duplicate
      entries marked setting the "__duplicate__" metadata field to True.

    """
    # This function is kept only for backwards compatibility.
    warnings.warn(
        "The find_duplicate_entries() function is kept only for "
        "backwards compatibility with import scripts that explicitly "
        "added it to the import hooks. It does not conform to the "
        "current way of implementing deduplication and it is not to "
        "be used.",
        stacklevel=2,
    )

    ret = []
    for filepath, entries, account, importer in extracted:
        # Sort the existing entries by date: find_similar_entries()
        # uses bisection to reduce the list of existing entries to the
        # set in a narrow date interval around the date of each entry
        # in the set it is comparing against.
        existing.sort(key=operator.attrgetter("date"))

        # Find duplicates.
        pairs = similar.find_similar_entries(entries, existing)

        # We could do something smarter than throwing away the
        # information about which entry is the source of the possible
        # duplication.
        duplicates = {id(duplicate) for duplicate, source in pairs}
        marked = []
        for entry in entries:
            if id(entry) in duplicates:
                meta = entry.meta.copy()
                meta[DUPLICATE] = True
                entry = entry._replace(meta=meta)
            marked.append(entry)
        ret.append((filepath, marked, account, importer))

        # Append the current batch of extracted entries to the
        # existing entries. This allows to deduplicate entries in the
        # current extraction run.
        existing.extend(marked)

    return ret


def mark_duplicate_entries(
    entries: data.Entries,
    existing: data.Entries,
    window: datetime.timedelta,
    compare: Callable[[data.Directive, data.Directive], bool],
) -> None:
    """Mark duplicate entries.

    Compare newly extracted entries to the existing entries. Only
    existing entries dated within the given time window around the
    date of the each existing entry.

    Entries that are determined to be duplicates of existing entries
    are marked setting the "__duplicate__" metadata field.

    Args:
      entries: Entries to be deduplicated.
      existing: Existing entries.
      window: Time window in which entries are compared.
      compare: Entry comparison function.

    """
    # The use of bisection to identify the entries in the existing
    # list that have dates within a given window around the date
    # of each newly extracted entry requires the existing entries
    # to be sorted by date.
    existing.sort(key=operator.attrgetter("date"))
    dates = [entry.date for entry in existing]

    def entries_date_window_iterator(date):
        lo = bisect.bisect_left(dates, date - window)
        hi = bisect.bisect_right(dates, date + window)
        for i in range(lo, hi):
            yield existing[i]

    for entry in entries:
        for target in entries_date_window_iterator(entry.date):
            if compare(entry, target):
                entry.meta[DUPLICATE] = target


def print_extracted_entries(extracted: List[ExtractedEntry], output: io.TextIOBase) -> None:
    """Print extracted entries.

    Entries marked as duplicates are printed as comments.

    Args:
      extracted: List of (filepath, entries, account, importer) tuples
        where entries is the list of entries extracted from the
        document at filepath by importer.
      output: A file object to write to. The object needs to implement
       a write() method that accepts an unicode string.

    """
    if extracted and HEADER:
        output.write(HEADER + "\n")

    for filepath, entries, account, importer in extracted:
        output.write(SECTION.format(filepath) + "\n\n")

        for entry in entries:
            duplicate = entry.meta.pop(DUPLICATE, False)
            string = printer.format_entry(entry)
            # If the entry is a duplicate, comment it out and report
            # of which other entry this is a duplicate.
            if duplicate:
                if isinstance(duplicate, type(entry)):
                    filename = duplicate.meta.get("filename")
                    lineno = duplicate.meta.get("lineno")
                    if filename and lineno:
                        output.write(f"; duplicate of {filename}:{lineno}\n")
                string = textwrap.indent(string, "; ")
            output.write(string)
            output.write("\n")

        output.write("\n")


def merge_extracted_entries(
    extracted: List[ExtractedEntry], relations: List[OrderingRelation] = ()
) -> Tuple[List[MergedEntry], List[MergeConflict]]:
    """Merge entries extracted from multiple documents in a single stream.

    Entries are ordered by date. Directive types that are effective at
    the beginning of the day, like balance assertions, are placed
    before other entries with the same date, as the ledger expects.
    Declared ordering relations between entries extracted for the same
    account on the same date are honored. Entries with the same date
    and no applicable relation are ordered by source document and line
    number so that the output is reproducible and does not depend on
    the order in which the documents were discovered.

    Args:
      extracted: List of (filepath, entries, account, importer) tuples
        where entries is the list of entries extracted from the
        document at filepath by importer.
      relations: List of (account, date, before, after) tuples
        declaring that the entry referenced by before must appear
        before the entry referenced by after in the merged output.
        Entry references are (filepath, lineno) tuples. Relations are
        only valid within the given account and date.

    Returns:
      A (merged, conflicts) tuple where merged is a list of (entry,
      filepath, account) tuples in output order and conflicts is a
      list of MergeConflict describing ordering relations that
      contradict each other. Entries involved in a conflict are
      omitted from the merged stream: emitting them in some arbitrary
      order would suggest a ordering that cannot be guaranteed.

    """
    # Flatten the extracted entries, remembering the document each
    # entry was extracted from.
    items = []
    for filepath, entries, account, _ in extracted:
        for entry in entries:
            items.append((entry, filepath, account))

    # Index entries by source document and line number. This is how
    # ordering relations refer to entries.
    indexed = {}
    for index, (entry, filepath, account) in enumerate(items):
        indexed.setdefault((filepath, entry.meta.get("lineno")), []).append(index)

    # Apply the ordering relations. Relations referencing entries that
    # are not part of this extraction run, that cannot be identified
    # unambiguously, or that were extracted for a different account or
    # date than the relation is scoped to, do not apply.
    successors = [set() for _ in items]
    applied = []
    for relation in relations:
        account, date, before, after = relation
        before_items = indexed.get(before, [])
        after_items = indexed.get(after, [])
        if len(before_items) != 1 or len(after_items) != 1:
            continue
        i, j = before_items[0], after_items[0]
        if items[i][2] != account or items[i][0].date != date:
            continue
        if items[j][2] != account or items[j][0].date != date:
            continue
        if j not in successors[i]:
            successors[i].add(j)
            applied.append((relation, i, j))

    # The base ordering key. Entries are ordered by date and directive
    # type, so that balance assertions and other directives effective
    # at the beginning of the day come first, then by source document
    # and line number and finally by content. This makes the ordering
    # of entries not constrained by relations fully reproducible.
    def key(index):
        entry, filepath, account = items[index]
        return (
            entry.date,
            data.SORT_ORDER.get(type(entry), 0),
            filepath,
            entry.meta.get("lineno") or 0,
            printer.format_entry(entry),
        )

    # Emit the entries, respecting the ordering relations. Among the
    # entries that can be emitted, the one with the smallest base
    # ordering key is picked, so the output does not depend on the
    # order in which the documents were discovered.
    indegree = [0] * len(items)
    for succ in successors:
        for j in succ:
            indegree[j] += 1
    heap = [(key(i), i) for i in range(len(items)) if indegree[i] == 0]
    heapq.heapify(heap)
    emitted = []
    while heap:
        _, i = heapq.heappop(heap)
        emitted.append(i)
        for j in successors[i]:
            indegree[j] -= 1
            if indegree[j] == 0:
                heapq.heappush(heap, (key(j), j))

    merged = [items[i] for i in emitted]
    if len(emitted) == len(items):
        return merged, []

    # The ordering relations are contradictory: the remaining entries
    # cannot be ordered. Identify the conflicting relations. A
    # relation conflicts when its "before" entry is reachable from its
    # "after" entry, that is when satisfying it requires violating
    # other relations.
    remaining = set(range(len(items))) - set(emitted)

    def reaches(start):
        seen = set()
        stack = [start]
        while stack:
            i = stack.pop()
            for j in successors[i]:
                if j in remaining and j not in seen:
                    seen.add(j)
                    stack.append(j)
        return seen

    conflicts = {}
    for relation, i, j in applied:
        if i in remaining and j in remaining and (i == j or i in reaches(j)):
            account, date, _, _ = relation
            group = conflicts.setdefault((account, date), {"relations": [], "entries": []})
            group["relations"].append(relation)
    for i in sorted(remaining, key=key):
        entry, filepath, account = items[i]
        group = conflicts.setdefault((account, entry.date), {"relations": [], "entries": []})
        group["entries"].append(items[i])

    result = []
    for (account, date), group in sorted(conflicts.items()):
        relations = sorted(set(group["relations"]))
        result.append(MergeConflict(account, date, relations, group["entries"]))

    return merged, result


def print_merged_entries(
    extracted: List[ExtractedEntry],
    output: io.TextIOBase,
    relations: List[OrderingRelation] = (),
) -> List[MergeConflict]:
    """Print entries extracted from multiple documents as a single stream.

    Entries from all the documents are merged and printed in the order
    in which they occurred, instead of being grouped by source
    document. Each entry is preceded by a comment recording the
    document and line number it was extracted from, so it can be
    traced back to its source. Entries marked as duplicates are
    printed as comments. Ordering relations that contradict each other
    are reported in comment sections and the entries involved are
    omitted from the output.

    Args:
      extracted: List of (filepath, entries, account, importer) tuples
        where entries is the list of entries extracted from the
        document at filepath by importer.
      output: A file object to write to. The object needs to implement
        a write() method that accepts an unicode string.
      relations: List of ordering relations between entries, see
        merge_extracted_entries().

    Returns:
      The list of MergeConflict describing ordering relations that
      could not be satisfied.

    """
    merged, conflicts = merge_extracted_entries(extracted, relations)

    if (merged or conflicts) and HEADER:
        output.write(HEADER + "\n")

    for conflict in conflicts:
        output.write(CONFLICT_SECTION.format(conflict.account, conflict.date) + "\n")
        for account, date, before, after in conflict.relations:
            output.write(f";;   {before[0]}:{before[1]} must come before {after[0]}:{after[1]}\n")
        output.write(";; The affected entries are omitted from this output:\n")
        for entry, filepath, account in conflict.entries:
            lineno = entry.meta.get("lineno")
            ref = f"{filepath}:{lineno}" if lineno is not None else filepath
            output.write(f";;   {ref}\n")
        output.write("\n")

    for entry, filepath, account in merged:
        duplicate = entry.meta.pop(DUPLICATE, False)
        lineno = entry.meta.get("lineno")
        source = f"{filepath}:{lineno}" if lineno is not None else filepath
        string = printer.format_entry(entry)
        # If the entry is a duplicate, comment it out and report
        # of which other entry this is a duplicate.
        if duplicate:
            if isinstance(duplicate, type(entry)):
                filename = duplicate.meta.get("filename")
                lineno = duplicate.meta.get("lineno")
                if filename and lineno:
                    output.write(f"; duplicate of {filename}:{lineno}\n")
            string = textwrap.indent(string, "; ")
        output.write(f"; source: {source}\n")
        output.write(string)
        output.write("\n")

    return conflicts
