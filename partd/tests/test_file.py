from partd.file import File

import shutil
import os

import pytest

from partd.file import is_published


def test_partd():
    with File() as p:
        p.append({'x': b'Hello', 'y': b'abc'})
        p.append({'x': b'World!', 'y': b'def'})
        assert os.path.exists(p.filename('x'))
        assert os.path.exists(p.filename('y'))

        result = p.get(['y', 'x'])
        assert result == [b'abcdef', b'HelloWorld!']

        assert p.get('z') == b''

        with p.lock:  # uh oh, possible deadlock
            result = p.get(['x'], lock=False)

    assert not os.path.exists(p.path)


def test_key_tuple():
    with File() as p:
        p.append({('a', 'b'): b'123'})
        assert os.path.exists(p.filename(('a', 'b')))


def test_iset():
    with File() as p:
        p.iset('x', b'123')
        assert 'x' in p._iset_seen
        assert 'y' not in p._iset_seen
        p.iset('x', b'123')
        p.iset('x', b'123')
        assert p.get('x') == b'123'


def test_nested_get():
    with File() as p:
        p.append({'x': b'1', 'y': b'2', 'z': b'3'})
        assert p.get(['x', ['y', 'z']]) == [b'1', [b'2', b'3']]


def test_drop():
    with File() as p:
        p.append({'x': b'123'})
        p.iset('y', b'abc')
        assert p.get('x') == b'123'
        assert p.get('y') == b'abc'

        p.drop()
        assert p.get('x') == b''
        assert p.get('y') == b''

        p.append({'x': b'123'})
        p.iset('y', b'def')
        assert p.get('x') == b'123'
        assert p.get('y') == b'def'


def test_del():
    f = File()

    assert f.path
    assert os.path.exists(f.path)

    f.__del__()
    assert not os.path.exists(f.path)

    with File('Foo') as p:
        p.__del__()
        assert os.path.exists(p.path)


def test_interrupted_mid_copy_is_not_published(tmp_path):
    """Stopping after some export files exist leaves no successful export."""
    from partd.file import ExportNotPublished, export_status, open_export
    import partd.file as file_module

    src = str(tmp_path / 'source')
    dst = str(tmp_path / 'export')
    os.makedirs(src)
    p = File(src)
    original = file_module._copy_snapshot_entry
    calls = []

    def interrupt_second(entry, target, fsync=False):
        calls.append(entry['key'])
        if len(calls) == 2:
            raise KeyboardInterrupt()
        return original(entry, target, fsync=fsync)

    try:
        p.append({'a': b'A' * 1000, 'b': b'B' * 2000, 'empty': b''})
        with pytest.raises(KeyboardInterrupt):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(file_module, '_copy_snapshot_entry',
                           interrupt_second)
                p.export(dst, keys=['a', 'b', 'empty'])

        # The first key was fully written, the rest were not.
        assert os.path.exists(os.path.join(dst, 'a'))
        assert not os.path.exists(os.path.join(dst, 'b'))
        assert not is_published(dst)
        status = export_status(dst)
        assert not status.published
        with pytest.raises(ExportNotPublished):
            open_export(dst)

        # The source partition still reads and writes normally.
        assert p.get(['a', 'b', 'empty']) == [b'A' * 1000, b'B' * 2000, b'']
        p.append({'a': b'+more'})
        assert p.get('a') == b'A' * 1000 + b'+more'

        # A retry publishes a complete export.
        view = p.export(dst, keys=['a', 'b', 'empty'])
        assert view.get(['a', 'b', 'empty']) == [
            b'A' * 1000 + b'+more', b'B' * 2000, b'']
        assert is_published(dst)
    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_specify_dirname():
    with File(dir=os.getcwd()) as f:
        assert os.getcwd() in f.path


def test_export_snapshot_isolated_from_append_and_rebuild(tmp_path):
    """Selected keys share one logical instant across append and rebuild."""
    from partd.file import MISSING, export_status, open_export
    import threading

    src = str(tmp_path / 'source')
    dst = str(tmp_path / 'export')
    os.makedirs(src)
    p = File(src)
    try:
        p.append({'big-a': b'A' * 1000, 'big-b': b'B' * 2000,
                  'empty': b''})
        assert p.get('missing') == b''

        copying = threading.Event()
        written = threading.Event()

        def writer():
            # One batch, both big keys, after the export range is captured.
            assert copying.wait(10)
            p.append({'big-a': b'-appended-a', 'big-b': b'-appended-b'})
            p.drop()
            p.append({'big-a': b'rebuilt-a', 'big-b': b'rebuilt-b',
                      'empty': b'not-empty-after-rebuild'})
            written.set()

        def pause_copy(snapshot):
            # The writer must finish while the export is still copying.
            copying.set()
            assert written.wait(10)

        thread = threading.Thread(target=writer)
        thread.start()
        view = p.export(dst, keys=['big-a', 'big-b', 'empty', 'missing'],
                        before_copy=pause_copy)
        thread.join()

        assert view.status('empty') == 'empty'
        assert view.status('missing') == 'missing'
        assert view.get(['big-a', 'big-b']) == [b'A' * 1000, b'B' * 2000]
        assert view.get('empty') == b''
        assert view.get('missing') is MISSING
        assert view.missing_keys == ['missing']
        assert view.empty_keys == ['empty']
        assert view.selected_keys() == ['big-a', 'big-b', 'empty', 'missing']
        assert view.verify() == []

        status = export_status(dst)
        assert status.published
        assert status.is_published
        assert status.reason is None

        reopened = open_export(dst)
        assert reopened.get(['big-a', 'big-b']) == [b'A' * 1000, b'B' * 2000]
        assert reopened.get('missing') is MISSING

        # The exported directory keeps the normal File filename layout.
        plain = File(dst)
        assert plain.get(['big-a', 'big-b']) == [b'A' * 1000, b'B' * 2000]
        assert plain.filename('big-a') == reopened.filename('big-a')

        # The original partition still reads and writes normally.
        assert p.get(['big-a', 'big-b', 'empty']) == [
            b'rebuilt-a', b'rebuilt-b', b'not-empty-after-rebuild']
        p.append({'big-a': b'+more'})
        assert p.get('big-a') == b'rebuilt-a+more'

        with pytest.raises(PermissionError):
            reopened.append({'big-a': b'nope'})
        with pytest.raises(PermissionError):
            reopened.drop()
    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_interrupted_export_is_not_accepted(tmp_path):
    from partd.file import ExportNotPublished, export_status, open_export

    src = str(tmp_path / 'source')
    dst = str(tmp_path / 'export')
    os.makedirs(src)
    p = File(src)
    try:
        p.append({'a': b'A' * 1000, 'b': b'B' * 2000, 'empty': b''})

        class StopCopy(Exception):
            pass

        def interrupt(snapshot):
            raise StopCopy()

        with pytest.raises(StopCopy):
            p.export(dst, keys=['a', 'b', 'empty'], before_copy=interrupt)

        status = export_status(dst)
        assert not status.published
        assert not status
        with pytest.raises(ExportNotPublished):
            open_export(dst)
        with pytest.raises(ExportNotPublished):
            File(dst, readonly=True)

        # A retry may reuse the destination and then reads normally.
        view = p.export(dst, keys=['a', 'b', 'empty'], overwrite=True)
        assert view.get(['a', 'b', 'empty']) == [b'A' * 1000, b'B' * 2000, b'']
        assert export_status(dst).published
    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)


def test_partial_export_files_are_not_published(tmp_path):
    """Files written before an interruption are never a successful export."""
    from partd.file import ExportNotPublished, open_export

    src = str(tmp_path / 'source')
    dst = str(tmp_path / 'export')
    os.makedirs(src)
    p = File(src)
    try:
        p.append({'a': b'A' * 1000, 'b': b'B' * 2000})

        class StopCopy(Exception):
            pass

        import partd.file as file_module
        original = file_module.os.replace

        def stop_on_success(src_path, dst_path):
            if os.path.basename(dst_path) == 'SUCCESS':
                raise StopCopy()
            return original(src_path, dst_path)

        with pytest.raises(StopCopy):
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(file_module.os, 'replace', stop_on_success)
                p.export(dst, keys=['a', 'b'])

        assert os.path.exists(os.path.join(dst, 'a'))
        assert os.path.exists(os.path.join(dst, 'b'))
        assert not os.path.exists(
            os.path.join(dst, '.partd-export@meta', 'SUCCESS'))
        with pytest.raises(ExportNotPublished):
            open_export(dst)

        # Retrying in place clears stale partial contents before publishing.
        view = p.export(dst, keys=['a'])
        assert view.get('a') == b'A' * 1000
        assert not os.path.exists(os.path.join(dst, 'b'))
        assert open_export(dst).selected_keys() == ['a']
    finally:
        shutil.rmtree(src, ignore_errors=True)
        shutil.rmtree(dst, ignore_errors=True)
