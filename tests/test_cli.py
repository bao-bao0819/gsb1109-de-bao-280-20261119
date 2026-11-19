import os
import json
import pathlib
import sqlite3
import argparse

import pytest

from csvw.__main__ import (
    csvw2json, csvw2datasette, csvwdescribe, csvwvalidate, csvw2markdown, csvw2sqlite)


def relpath(fname):
    return str(pathlib.Path(__file__).parent.joinpath('fixtures', fname).relative_to(os.getcwd()))


@pytest.fixture
def csvname():
    return relpath('csv.txt')


@pytest.fixture
def tsvname():
    return relpath('test.tsv')


@pytest.fixture
def mdname():
    return relpath('csv.txt-metadata.json')


@pytest.fixture
def multitable_mdname():
    return relpath('multitable/metadata.json')


def run(func, **kw):
    return func(argparse.Namespace(**kw), test=True)


def test_csvw2json(csvname, mdname, capsys):
    run(csvw2json, url=csvname)
    out, _ = capsys.readouterr()
    assert json.loads(out)
    run(csvw2json, url=mdname)
    out, _ = capsys.readouterr()
    assert json.loads(out)
    run(csvw2json, url=relpath('no-metadata.csv'))
    out, _ = capsys.readouterr()
    assert json.loads(out)


def test_csvw2markdown(mdname, multitable_mdname, capsys):
    assert run(csvw2markdown, url=mdname) == 0
    out, _ = capsys.readouterr()
    assert 'Described by' in out

    assert run(csvw2markdown, url=multitable_mdname) == 0
    out, _ = capsys.readouterr()
    assert 'References' in out


def test_csvwvalidate_2(multitable_mdname):
    assert run(csvwvalidate, url=multitable_mdname, verbose=True, lax=False) == 2
    assert run(csvwvalidate, url=multitable_mdname, verbose=True, lax=True) == 0


def test_csvwvalidate(mdname, tmp_path):
    assert run(csvwvalidate, url=mdname, lax=False) == 0
    p = tmp_path / 'md.json'
    p.write_text(pathlib.Path(mdname).read_text(encoding='utf8').replace('@context', 'context'))
    assert run(csvwvalidate, url=str(p), verbose=True, lax=False) == 2

    p.write_text(pathlib.Path(mdname).read_text(encoding='utf8').replace('"en"', '1'))
    assert run(csvwvalidate, url=str(p), verbose=True, lax=False) == 1


def test_csvwdescribe(csvname, tsvname, capsys):
    run(csvwdescribe, csv=[csvname], delimiter=',')
    out, _ = capsys.readouterr()
    assert json.loads(out)

    run(csvwdescribe, csv=[tsvname, csvname], delimiter=None)
    out, _ = capsys.readouterr()
    assert json.loads(out)


def test_csvw2datasette(tmp_path, mdname):
    run(csvw2datasette, url=mdname, outdir=tmp_path)
    assert tmp_path.joinpath('datasette.db').exists()


def test_csvw2sqlite(tmp_path, mdname):
    out = tmp_path / 'db.sqlite'
    run(csvw2sqlite, url=mdname, output=out)
    assert out.exists()
    conn = sqlite3.connect(out)
    cu = conn.cursor()
    cu.execute('select count(*) from `csv.txt`')
    assert cu.fetchone()[0] == 2
    conn.close()


def test_csvwvalidate_columns(tmp_path, capsys):
    md = tmp_path / 'md.json'
    md.write_text(json.dumps({
        "@context": "http://www.w3.org/ns/csvw",
        "tables": [
            {
                "url": "items.csv",
                "tableSchema": {
                    "columns": [
                        {"name": "ID", "datatype": "string"},
                        {"name": "when",
                         "datatype": {"base": "date", "format": "yyyy-MM-dd"}},
                        {"name": "comment",
                         "datatype": {"base": "string", "format": "[a-z]+"}},
                        {"name": "FK", "datatype": "string"},
                    ],
                    "foreignKeys": [{
                        "columnReference": ["FK"],
                        "reference": {"resource": "targets.csv", "columnReference": ["ID"]},
                    }],
                },
            },
            {
                "url": "targets.csv",
                "tableSchema": {
                    "columns": [
                        {"name": "ID", "datatype": "string"},
                        {"name": "notes",
                         "datatype": {"base": "string", "format": "[a-z]+"}},
                    ],
                },
            },
        ],
    }), encoding='utf-8')
    (tmp_path / 'targets.csv').write_text("ID,notes\nt1,Broken Notes!\n", encoding='utf-8')
    items = tmp_path / 'items.csv'
    items.write_text("ID,when,comment,FK\ni1,2026-11-19,Not lowercase!,t1\n", encoding='utf-8')

    # Invalid formats in unselected columns do not affect validation:
    assert run(csvwvalidate, url=str(md), verbose=True, lax=False,
               columns=['ID', 'when']) == 0
    # Per-table column selections work, too:
    assert run(csvwvalidate, url=str(md), verbose=True, lax=False,
               columns=['items.csv:ID,when', 'targets.csv:ID']) == 0
    # ... while selecting a broken column fails validation:
    assert run(csvwvalidate, url=str(md), verbose=True, lax=False,
               columns=['ID', 'comment']) != 0

    # A broken foreign key is still caught - and reported with location:
    items.write_text("ID,when,comment,FK\ni1,2026-11-19,ok,t9\n", encoding='utf-8')
    assert run(csvwvalidate, url=str(md), verbose=True, lax=False, columns=['ID']) == 2
    out, _ = capsys.readouterr()
    assert 'items.csv:2' in out
