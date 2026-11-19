Setup
-----

  >>> from os import mkdir, path, unlink
  >>> from shutil import rmtree
  >>> from tempfile import mkdtemp
  >>> import click.testing
  >>> import beangulp
  >>> from beangulp.tests.utils import Importer

An importer to create error conditions:

  >>> class ErrorImporter(Importer):
  ...
  ...     def identify(self, filepath):
  ...         name = path.basename(filepath)
  ...         if name == 'error.foo':
  ...             return True
  ...         return False
  ...
  ...     def filename(self, filepath):
  ...         return 'bbb.csv'

Test harness:

  >>> importers = [
  ...     Importer('test.ImporterA', 'Assets:Tests', 'text/csv'),
  ...     ErrorImporter('test.ImporterB', 'Assets:Tests', None),
  ... ]
  >>> runner = click.testing.CliRunner()
  >>> def run(*args):
  ...     ingest = beangulp.Ingest(importers)
  ...     return runner.invoke(ingest.cli, args, catch_exceptions=False)

Create a dowloads and a documents directory:

  >>> temp = mkdtemp()
  >>> downloads = path.join(temp, 'downloads')
  >>> documents = path.join(temp, 'documents')
  >>> mkdir(downloads)
  >>> mkdir(documents)


Tests
-----

The basics:

  >>> r = run('archive', '--help')
  >>> r.exit_code
  0
  >>> print(r.output)
  Usage: beangulp archive [OPTIONS] [SRC]...

Test with an empty downloads directory:

  >>> r = run('archive', downloads)
  >>> r.exit_code
  0
  >>> print(r.output)

Add some documents:

  >>> fnames = ['aaa.txt', 'bbb.csv', 'zzz.txt']
  >>> for fname in fnames:
  ...     with open(path.join(downloads, fname), 'w') as f:
  ...         pass

Run in dry-run mode:
  
  >>> r = run('archive', downloads, '-o', documents, '-n')
  >>> r.exit_code
  0
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... OK
    .../documents/Assets/Tests/1970-01-01.bbb.csv
  * .../downloads/zzz.txt

No files have actually been moved:

  >>> path.exists(path.join(downloads, 'bbb.csv'))
  True
  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.bbb.csv'))
  False

Now for real:

  >>> r = run('archive', downloads, '-o', documents)
  >>> r.exit_code
  0
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... OK
    .../documents/Assets/Tests/1970-01-01.bbb.csv
  * .../downloads/zzz.txt

  >>> path.exists(path.join(downloads, 'bbb.csv'))
  False
  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.bbb.csv'))
  True

Trying to move a documents over an exisiting file:

  >>> with open(path.join(downloads, 'bbb.csv'), 'w') as f:
  ...     pass

  >>> r = run('archive', downloads, '-o', documents)
  >>> r.exit_code
  1
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... ERROR
    .../documents/Assets/Tests/1970-01-01.bbb.csv
    Destination file already exists.
  * .../downloads/zzz.txt
  # Errors detected: documents will not be filed.

Cleanup documents directory:

  >>> rmtree(documents)
  >>> mkdir(documents)
  
Collision in destination filename:

  >>> fnames = ['aaa.txt', 'bbb.csv', 'zzz.txt', 'error.foo']
  >>> for fname in fnames:
  ...     with open(path.join(downloads, fname), 'w') as f:
  ...         pass

  >>> r = run('archive', downloads, '-o', documents)
  >>> r.exit_code
  1
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... OK
    .../documents/Assets/Tests/1970-01-01.bbb.csv
  * .../downloads/error.foo ... ERROR
    .../documents/Assets/Tests/1970-01-01.bbb.csv
    Collision in destination file path.
  * .../downloads/zzz.txt
  # Errors detected: documents will not be filed.


Cleanup
-------

  >>> rmtree(temp)


Resumable archival with a receipt
----------------------------------

A receipt records which documents have been extracted and which have
been filed. The archive command files only documents recorded as
pending, and a failed move can be retried without moving the documents
that already succeeded.

Create fresh directories and extract with a receipt:

  >>> temp = mkdtemp()
  >>> downloads = path.join(temp, 'downloads')
  >>> documents = path.join(temp, 'documents')
  >>> receipt = path.join(temp, 'receipt.json')
  >>> mkdir(downloads)
  >>> mkdir(documents)
  >>> fnames = ['aaa.csv', 'bbb.csv']
  >>> for fname in fnames:
  ...     with open(path.join(downloads, fname), 'w') as f:
  ...         pass

  >>> output = path.join(temp, 'output.beancount')
  >>> r = run('extract', downloads, '-o', output, '--receipt', receipt)
  >>> r.exit_code
  0

Each extracted document is recorded as pending:

  >>> import json
  >>> with open(receipt) as f:
  ...     data = json.load(f)
  >>> sorted(record['status'] for record in data['files'].values())
  ['pending', 'pending']

Add an unrelated download after extraction. It is not recorded and is
left alone by the archive command:

  >>> with open(path.join(downloads, 'not-extracted.csv'), 'w') as f:
  ...     pass

  >>> r = run('archive', downloads, '-o', documents, '--receipt', receipt)
  >>> r.exit_code
  0
  >>> path.exists(path.join(downloads, 'not-extracted.csv'))
  True

Both recorded documents are now archived:

  >>> with open(receipt) as f:
  ...     data = json.load(f)
  >>> sorted(record['status'] for record in data['files'].values())
  ['archived', 'archived']

Running archive again does not move those documents a second time:

  >>> for fname in ['aaa.csv', 'bbb.csv']:
  ...     with open(path.join(downloads, fname), 'w') as f:
  ...         pass
  >>> r = run('archive', downloads, '-o', documents, '--receipt', receipt)
  >>> r.exit_code
  0
  >>> path.exists(path.join(downloads, 'aaa.csv'))
  True

Cleanup:

  >>> rmtree(temp)
