__copyright__ = "Copyright (C) 2016-2017  Martin Blais"
__license__ = "GNU GPLv2"

import io
import bisect
import datetime
import operator
import textwrap
import warnings

from typing import Callable
from typing import TYPE_CHECKING
from typing import Tuple
from typing import List
from typing import Optional
from typing import Iterable

from beancount.core import data
from beancount.parser import printer
from beangulp import similar
from beangulp.exceptions import EntryParseError
from beangulp.exceptions import FileOpenError
from beangulp.exceptions import NoImporterError

if TYPE_CHECKING:
    from beangulp.importer import Importer

# Header for the file where the extracted entries are written.
HEADER = ";; -*- mode: beancount -*-\n"

# Format for the section titles separating entries extracted from
# different documents. This is used as a format sting passing the
# document filesystem path as argument.
SECTION = "**** {}"

# Metadata field that indicates the entry is a likely duplicate.
DUPLICATE = "__duplicate__"

ExtractedEntry = Tuple[str, data.Entries, data.Account, "Importer"]


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

    Raises:
      beangulp.exceptions.FileOpenError: The importer could not open
        or read the document.
      beangulp.exceptions.EntryParseError: The importer could read the
        document but could not interpret one of its records.

    """
    try:
        entries = importer.extract(filename, existing_entries)
    except FileOpenError:
        raise
    except EntryParseError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        # Errors opening or reading the document surface as OSError
        # (including its FileNotFoundError and PermissionError
        # subclasses) or UnicodeDecodeError from importer code.
        raise FileOpenError(f"Could not read document {filename}.") from exc
    if not entries:
        return []

    # Sort the newly imported entries.
    importer.sort(entries)

    # Ensure that the entries are typed correctly.
    for entry in entries:
        data.sanity_check_types(entry)

    return entries


# A document that could not be processed, associated with the error
# that prevented its processing. The first element is the document
# filesystem path, the second element is the error exception.
DocumentError = Tuple[str, Exception]


def process_documents(
    importers: List["Importer"],
    documents: Iterable[str],
    existing_entries: "data.Directives",
    on_error: Callable[[str, Exception], None],
    on_pending: Optional[Callable[[str, NoImporterError], None]] = None,
    on_success: Optional[Callable[[str], None]] = None,
    failfast: bool = False,
) -> Tuple[List[ExtractedEntry], List[DocumentError], List[DocumentError]]:
    """Identify importers and extract entries from a batch of documents.

    Each document is sorted in one of three categories:

    * documents for which no configured importer applies are reported
      through on_pending with a NoImporterError and added to the
      returned pending list;
    * documents that could not be opened or read
      (beangulp.exceptions.FileOpenError), or for which an importer
      could not interpret a record
      (beangulp.exceptions.EntryParseError), are reported through
      on_error and added to the returned errors list;
    * documents successfully processed contribute entries to the
      returned extracted list and are reported through on_success, if
      one is provided.

    A failure processing a document does not prevent the remaining
    documents from being processed.

    Args:
      importers: List of importer instances.
      documents: Filesystem paths of the documents to process.
      existing_entries: Existing entries.
      on_error: Callback invoked with the document path and the error
        exception for each document that could not be processed.
      on_pending: Optional callback invoked with the document path for
        each document for which no importer applies.
      on_success: Optional callback invoked with the document path for
        each successfully processed document.
      failfast: When True, stop processing at the first document that
        triggers an error.

    Returns:
      A tuple containing the list of extracted entries, the list of
      (document, NoImporterError) pairs for documents without a
      matching importer, and the list of (document, error) pairs for
      the documents that triggered an error.

    """
    from beangulp import identify

    extracted: List[ExtractedEntry] = []
    pending: List[DocumentError] = []
    errors: List[DocumentError] = []

    for filename in documents:
        try:
            importer = identify.identify(importers, filename)
        except (OSError, UnicodeDecodeError) as read_error:
            # An importer tried and failed to open or read the
            # document while identifying it.
            error = FileOpenError(f"Could not read document {filename}.")
            error.__cause__ = read_error
            errors.append((filename, error))
            on_error(filename, error)
            if failfast:
                break
            continue
        except Exception as exc:
            # Other errors raised while identifying a document, for
            # example importer identification collisions.
            errors.append((filename, exc))
            on_error(filename, exc)
            if failfast:
                break
            continue

        if importer is None:
            # No configured importer can handle this document.
            exc = NoImporterError(f"No importer matches document {filename}.")
            pending.append((filename, exc))
            if on_pending is not None:
                on_pending(filename, exc)
            continue

        try:
            entries = extract_from_file(importer, filename, existing_entries)
            account = importer.account(filename)
        except Exception as exc:
            # Beangulp validation errors are classified by their
            # exception type. Any other exception is an unexpected
            # error in importer code. Processing continues with the
            # next document in either case.
            errors.append((filename, exc))
            on_error(filename, exc)
            if failfast:
                break
            continue

        extracted.append((filename, entries, account, importer))
        if on_success is not None:
            on_success(filename)

    return extracted, pending, errors


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
