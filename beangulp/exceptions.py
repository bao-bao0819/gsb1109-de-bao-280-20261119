import contextlib
import textwrap
import traceback


class Error(RuntimeError):
    def __init__(self, message, *args):
        self.message = message
        self.args = args

    def __str__(self):
        return "\n".join((*self.args, self.message))


class NoImporterError(Error):
    """No configured importer can handle a document."""


class FileOpenError(Error):
    """An importer cannot open or read a document."""


class EntryParseError(Error):
    """An importer cannot interpret a record read from a document."""


class ExceptionsTrap(contextlib.AbstractContextManager):
    """A context manager to log exceptions.

    Works similarly to contextlib.suppress() but logs the exceptions
    instead than simply discarding them. This is used to shorten and
    unify exception handling in the command line wrappers. The format
    of the reporting is specific to the Beangulp command line
    interface.

    """

    def __init__(self, log):
        self.log = log
        self.errors = 0

    def report(self, exc):
        """Log an exception and count it as an error.

        Args:
          exc: The exception instance to report.

        """
        self.errors += 1
        self.log("  ERROR", fg="red")
        if isinstance(exc, Error):
            # Beangulp validation error.
            self.log(textwrap.indent(str(exc), "  "))
            return
        # Unexpected exception.
        self.log("  Exception in importer code.")
        exc_str = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        self.log(textwrap.indent(exc_str, "  ").rstrip())

    def __exit__(self, exctype, excinst, exctb):
        if exctype is None:
            return True
        if not isinstance(excinst, Exception):
            # Do not suppress BaseException instances, for example
            # KeyboardInterrupt or SystemExit.
            return False
        self.report(excinst)
        return True

    def __bool__(self):
        """Return True if any error occurred."""
        return self.errors != 0
