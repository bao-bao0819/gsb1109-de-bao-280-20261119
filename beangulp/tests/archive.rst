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

A re-download with the same name and identical content is not filed again:

  >>> with open(path.join(downloads, 'bbb.csv'), 'w') as f:
  ...     pass

  >>> r = run('archive', downloads, '-o', documents)
  >>> r.exit_code
  0
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... DUPLICATE
    .../documents/Assets/Tests/1970-01-01.bbb.csv
  * .../downloads/zzz.txt

  >>> path.exists(path.join(downloads, 'bbb.csv'))
  False
  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.bbb.csv'))
  True
  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.bbb.1.csv'))
  False

A re-download with corrected content keeps both versions:

  >>> with open(path.join(downloads, 'bbb.csv'), 'w') as f:
  ...     _ = f.write('corrected')

  >>> r = run('archive', downloads, '-o', documents)
  >>> r.exit_code
  0
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... OK
    .../documents/Assets/Tests/1970-01-01.bbb.1.csv
  * .../downloads/zzz.txt

  >>> path.exists(path.join(downloads, 'bbb.csv'))
  False
  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.bbb.csv'))
  True
  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.bbb.1.csv'))
  True

Both versions can be retrieved by content:

  >>> with open(path.join(documents, 'Assets/Tests/1970-01-01.bbb.csv')) as f:
  ...     print(f.read())

  >>> with open(path.join(documents, 'Assets/Tests/1970-01-01.bbb.1.csv')) as f:
  ...     print(f.read())
  corrected

Filing the same corrected re-download again remains a duplicate:

  >>> with open(path.join(downloads, 'bbb.csv'), 'w') as f:
  ...     _ = f.write('corrected')

  >>> r = run('archive', downloads, '-o', documents)
  >>> r.exit_code
  0
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... DUPLICATE
    .../documents/Assets/Tests/1970-01-01.bbb.1.csv
  * .../downloads/zzz.txt

  >>> path.exists(path.join(downloads, 'bbb.csv'))
  False

Cleanup documents directory:

  >>> rmtree(documents)
  >>> mkdir(documents)

Two files in the same run with the same canonical name are versioned:

  >>> mkdir(path.join(downloads, 'again'))
  >>> for fname, content in [('bbb.csv', 'one'), ('again/bbb.csv', 'two')]:
  ...     with open(path.join(downloads, fname), 'w') as f:
  ...         _ = f.write(content)

  >>> r = run('archive', downloads, '-o', documents)
  >>> r.exit_code
  0
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... OK
    .../documents/Assets/Tests/1970-01-01.bbb.csv
  * .../downloads/zzz.txt
  * .../downloads/again/bbb.csv ... OK
    .../documents/Assets/Tests/1970-01-01.bbb.1.csv

  >>> rmtree(path.join(downloads, 'again'))

Collision with a different file requires an explicit overwrite:

  >>> with open(path.join(documents, 'Assets/Tests/1970-01-01.bbb.csv')) as f:
  ...     original = f.read()

  >>> with open(path.join(downloads, 'bbb.csv'), 'w') as f:
  ...     _ = f.write('replacement')

  >>> r = run('archive', downloads, '-o', documents, '-f')
  >>> r.exit_code
  0
  >>> print(r.output)
  * .../downloads/aaa.txt
  * .../downloads/bbb.csv ... REPLACE
    .../documents/Assets/Tests/1970-01-01.bbb.csv
  * .../downloads/zzz.txt

  >>> with open(path.join(documents, 'Assets/Tests/1970-01-01.bbb.csv')) as f:
  ...     print(f.read())
  replacement

  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.bbb.1.csv'))
  True

An error while processing leaves all documents unfiled:

  >>> class FailingImporter(Importer):
  ...     def identify(self, filepath):
  ...         return path.basename(filepath) == 'failing.foo'
  ...     def filename(self, filepath):
  ...         return path.join('nested', 'name.pdf')

  >>> rmtree(documents)
  >>> mkdir(documents)
  >>> with open(path.join(downloads, 'failing.foo'), 'w') as f:
  ...     _ = f.write('unfiled')
  >>> failing_importer = FailingImporter('test.Failing', 'Assets:Tests', None)
  >>> failing = beangulp.Ingest(importers + [failing_importer])
  >>> r = runner.invoke(
  ...     failing.cli, ['archive', downloads, '-o', documents], catch_exceptions=False
  ... )

  >>> r.exit_code
  1
  >>> 'ERROR' in r.output
  True
  >>> 'contains path separator' in r.output
  True
  >>> '# Errors detected: documents will not be filed.' in r.output
  True

  >>> path.exists(path.join(downloads, 'aaa.txt'))
  True
  >>> path.exists(path.join(documents, 'Assets/Tests/1970-01-01.aaa.txt'))
  False


Cleanup
-------

  >>> rmtree(temp)
