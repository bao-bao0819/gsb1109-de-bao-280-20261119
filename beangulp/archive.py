__copyright__ = "Copyright (C) 2016  Martin Blais"
__license__ = "GNU GPLv2"

import errno
import os
import re
import shutil
import tempfile
import time
from typing import Dict, Optional, Tuple

from beangulp import utils
from beangulp.importer import Importer
from beangulp.exceptions import Error


# Template for the documents archival name.
FILENAME = "{date:%Y-%m-%d}.{name}"

# Actions returned by resolve().
ARCHIVE = "archive"
DUPLICATE = "duplicate"
REPLACE = "replace"


def filepath(importer: Importer, filepath: str) -> str:
    """Compute filing path for a document.

    The path mirrors the structure of the accounts associated to the
    documents and with a file name composed by the document date and
    document name returned by the importer.

    Args:
      importer: The importer instance to handle the document.
      filepath: Filesystem path to the document.

    Returns:
      Filing tree location where to store the document.

    Raises:
      beangulp.exceptions.Error: The canonical file name returned by
      the importer contains a path separator charachter or seems to
      contain a date.

    """
    # Get the account corresponding to the file.
    account = importer.account(filepath)
    filename = importer.filename(filepath) or os.path.basename(filepath)
    date = importer.date(filepath) or utils.getmdate(filepath)

    # The returned filename cannot contain the file path separator character.
    if os.sep in filename:
        raise Error("The filename contains path separator character.")

    if re.match(r"\d\d\d\d-\d\d-\d\d\.", filename):
        raise Error("The filename contains what looks like a date.")

    # Prepend account directory and date prefix.
    filename = os.path.join(
        account.replace(":", os.sep), FILENAME.format(date=date, name=filename)
    )

    return filename


def file_checksum(filepath: str) -> str:
    """Return the content checksum of the file at filepath."""
    return utils.sha1sum(filepath)


def versioned_filepath(filepath: str, version: int) -> str:
    """Return the filing path for a numbered version of a document.

    Version zero is the unmodified filing path. Higher versions are
    inserted before the file extension, for example
    ``2016-02-15.statement.1.pdf``.

    """
    if version == 0:
        return filepath
    root, ext = os.path.splitext(filepath)
    return f"{root}.{version}{ext}"


def _version_regexp(filepath: str):
    root, ext = os.path.splitext(os.path.basename(filepath))
    return re.compile(r"^{}\.([1-9]\d*){}$".format(re.escape(root), re.escape(ext)))


def _version_index(filepath: str, candidate: str) -> Optional[int]:
    if candidate == filepath:
        return 0
    if os.path.dirname(os.path.abspath(candidate)) != os.path.dirname(
        os.path.abspath(filepath)
    ):
        return None
    match = _version_regexp(filepath).match(os.path.basename(candidate))
    return int(match.group(1)) if match else None


def _existing_versions(filepath: str) -> Dict[int, str]:
    """Return the existing numbered versions of a filing path.

    Version zero is the filing path itself. Versioned files use the
    ``<name>.<version><extension>`` naming scheme produced by
    versioned_filepath().

    """
    directory = os.path.dirname(filepath) or os.curdir
    versions = {}
    if os.path.lexists(filepath):
        versions[0] = filepath
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return versions
    regexp = _version_regexp(filepath)
    for name in names:
        match = regexp.match(name)
        if match:
            versions.setdefault(int(match.group(1)), os.path.join(directory, name))
    return versions


def resolve(
    filepath: str,
    checksum: str,
    reserved: Optional[Dict[str, str]] = None,
    overwrite: bool = False,
) -> Tuple[str, str]:
    """Resolve where to file a document by comparing file contents.

    Existing versions of the filing path are compared by content. An
    identical file is reported as a duplicate; a different file is
    filed under the next available version number so that both
    versions remain available.

    Args:
      filepath: The canonical filing path for the document.
      checksum: Content checksum of the document to be filed.
      reserved: Mapping of paths already planned in the current batch
        to their content checksums. This makes version allocation
        atomic with respect to the documents in a single run.
      overwrite: When true, a different file at the canonical path is
        replaced rather than versioned. Files with identical content
        are still deduplicated.

    Returns:
      An (action, path) tuple. Action is one of ARCHIVE, DUPLICATE or
      REPLACE. For duplicates, path is the location of the identical
      file. For the other actions, path is the location where the
      document should be filed.

    """
    reserved = reserved or {}
    existing = _existing_versions(filepath)

    known: Dict[int, Tuple[str, Optional[str]]] = {
        version: (path, None) for version, path in existing.items()
    }
    for path, digest in reserved.items():
        version = _version_index(filepath, path)
        if version is not None:
            known[version] = (path, digest)

    # Look for an existing copy with identical contents. Compare with
    # both files already on disk and files planned in this batch.
    for version in sorted(known):
        path, digest = known[version]
        if digest is None:
            try:
                digest = file_checksum(path)
            except OSError:
                continue
        if digest == checksum:
            return DUPLICATE, path

    # An explicit overwrite only applies to the canonical path and
    # never to a path already claimed by another document in this run.
    if overwrite and 0 in existing and filepath not in reserved:
        return REPLACE, filepath

    # Allocate the lowest unused version number.
    version = 0
    while version in known:
        version += 1
    return ARCHIVE, versioned_filepath(filepath, version)


def _same_file(path1: str, path2: str) -> bool:
    try:
        return os.path.samefile(path1, path2)
    except OSError:
        return os.path.abspath(path1) == os.path.abspath(path2)


# Temporary files left behind by a process killed in the middle of a
# copy. They are only removed after they are old enough that no
# concurrent archive run can still own them.
TEMP_PREFIX = ".beangulp-"
TEMP_SUFFIX = ".tmp"
TEMP_MAX_AGE = 24 * 60 * 60


def cleanup(destination: str):
    """Remove stale temporary files left by interrupted archive runs.

    Only files using the temporary naming scheme produced by move()
    and older than TEMP_MAX_AGE are removed, so an interrupted run
    never loses the archived version or the source document and a
    concurrent run is not disturbed. Failures to remove a file are
    ignored: the file is merely a leftover and never an archived
    document.

    Args:
      destination: Root of the documents tree to clean up.

    """
    now = time.time()
    for root, _, names in os.walk(destination):
        for name in names:
            if not (name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)):
                continue
            filepath = os.path.join(root, name)
            try:
                if now - os.path.getmtime(filepath) < TEMP_MAX_AGE:
                    continue
                os.unlink(filepath)
            except OSError:
                pass


def move(
    src: str,
    dst: str,
    checksum: Optional[str] = None,
    overwrite: bool = False,
):
    """Move a file into place without ever exposing a partial file.

    The source is copied to a temporary file in the destination
    directory, its content is verified when a checksum is supplied,
    and only then the temporary file is linked or renamed to the final
    destination. If the process is interrupted, the previous
    destination file remains untouched and the source file is still
    present. A later run can then complete the archival.

    The destination directory, and all intermediate path segments, are
    created if they do not exist. The move is performed by copying and
    removing the source to support moving across filesystems.

    Args:
      src: Filesystem path of the file to move.
      dst: Destination filesystem path.
      checksum: Optional expected content checksum of src.
      overwrite: When true, atomically replace an existing dst.

    Raises:
      beangulp.exceptions.Error: The destination already exists, the
        source changed while being copied, or the content checksum
        does not match.

    """
    directory = os.path.dirname(dst) or os.curdir
    os.makedirs(directory, exist_ok=True)

    fd, temp = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=directory)
    try:
        with os.fdopen(fd, "wb") as outfile:
            with open(src, "rb") as infile:
                shutil.copyfileobj(infile, outfile)
                outfile.flush()
                os.fsync(outfile.fileno())
        shutil.copystat(src, temp)

        if checksum is not None and file_checksum(temp) != checksum:
            raise Error("Source file changed during archiving.", src)

        if overwrite:
            os.replace(temp, dst)
            temp = None
        else:
            try:
                os.link(temp, dst)
            except FileExistsError:
                raise Error("Destination file already exists.", dst)
            except OSError as exc:
                unsupported = {
                    errno.EPERM,
                    errno.EACCES,
                    errno.EMLINK,
                    errno.ENOSYS,
                    getattr(errno, "ENOTSUP", errno.EPERM),
                    getattr(errno, "EOPNOTSUPP", errno.EPERM),
                }
                if exc.errno not in unsupported:
                    raise
                # Some filesystems do not support hard links. Fall back
                # to an atomic rename after checking the destination.
                if os.path.lexists(dst):
                    raise Error("Destination file already exists.", dst)
                os.rename(temp, dst)
                temp = None
            else:
                os.unlink(temp)
                temp = None
    finally:
        if temp is not None and os.path.lexists(temp):
            os.unlink(temp)

    if not _same_file(src, dst):
        try:
            os.unlink(src)
        except FileNotFoundError:
            pass


def discard(src: str, existing: str):
    """Remove a source file whose content is already archived.

    The contents are compared again immediately before removal, so a
    file modified after planning is not lost.

    Args:
      src: Source file to remove.
      existing: Existing archived file with identical content.

    Raises:
      beangulp.exceptions.Error: The source content no longer matches
        the archived copy.

    """
    if not os.path.exists(src) or _same_file(src, existing):
        return
    if file_checksum(src) != file_checksum(existing):
        raise Error("Duplicate file content changed before removal.", src)
    os.unlink(src)
