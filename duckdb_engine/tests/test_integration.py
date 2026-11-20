import duckdb
import pandas as pd
from pytest import mark, raises
from sqlalchemy import __version__, text
from sqlalchemy.engine import create_engine
from sqlalchemy.engine.base import Connection
from sqlalchemy.exc import ProgrammingError

from duckdb_engine import RegistrationScope, register_dataframe

df = pd.DataFrame([{"a": 1}])


def _frame(data):
    return pd.DataFrame({k: pd.Series(v, dtype=object) for k, v in data.items()})


def _read(conn, view_name):
    return conn.execute(text("select * from {}".format(view_name))).fetchall()


def _assert_missing(conn, view_name):
    with raises(ProgrammingError, match="does not exist"):
        _read(conn, view_name)


@mark.skipif(not hasattr(Connection, "exec_driver_sql"), reason="Needs exec_driver_sql")
def test_register_driver(conn: Connection) -> None:
    conn.exec_driver_sql("register", ("test_df_driver", df))  # type: ignore[arg-type]
    conn.execute(text("select * from test_df_driver"))


def test_plain_register(conn: Connection) -> None:
    if __version__.startswith("1.3"):
        conn.execute(text("register"), {"name": "test_df", "df": df})
    else:
        conn.execute(text("register(:name, :df)"), {"name": "test_df", "df": df})
    conn.execute(text("select * from test_df"))


def test_scope_defaults_to_shared_registration() -> None:
    engine = create_engine("duckdb:///:memory:")
    shared = _frame({"a": ["shared"]})

    with engine.connect() as conn:
        register_dataframe(conn, "rates", shared)

    with engine.connect() as conn:
        assert _read(conn, "rates") == [("shared",)]


def test_borrow_scope_is_cleaned_on_success() -> None:
    engine = create_engine("duckdb:///:memory:")
    shared = _frame({"a": ["shared"]})
    temporary = _frame({"a": ["temporary"]})

    with engine.connect() as conn:
        register_dataframe(conn, "rates", shared)
        register_dataframe(conn, "scratch", temporary, scope="borrow")
        assert _read(conn, "scratch") == [("temporary",)]

    with engine.connect() as conn:
        assert _read(conn, "rates") == [("shared",)]
        _assert_missing(conn, "scratch")


def test_borrow_scope_restores_shadowed_shared_registration() -> None:
    engine = create_engine("duckdb:///:memory:")
    shared = _frame({"region": ["admin"], "rate": [1]})
    temporary = _frame({"region": ["a"], "rate": [2]})

    with engine.connect() as conn:
        register_dataframe(conn, "rates", shared)

    with engine.connect() as conn:
        register_dataframe(conn, "rates", temporary, scope=RegistrationScope.BORROW)
        assert _read(conn, "rates") == [("a", 2)]

    with engine.connect() as conn:
        assert _read(conn, "rates") == [("admin", 1)]


def test_borrow_scope_cleanup_after_failed_query(tmp_path) -> None:
    engine = create_engine(
        "duckdb:///{}".format(tmp_path / "registry.duckdb"),
        pool_size=1,
        max_overflow=0,
    )
    admin_rates = _frame({"region": ["admin"], "rate": [1]})
    report_a_rates = _frame({"region": ["a"], "rate": [2]})
    report_a_scratch = _frame({"value": ["a-scratch"]})
    report_b_scratch = _frame({"value": ["b-scratch"]})

    with engine.connect() as conn:
        register_dataframe(conn, "rates", admin_rates)

    report_a = engine.connect()
    register_dataframe(report_a, "rates", report_a_rates, scope="borrow")
    register_dataframe(report_a, "scratch", report_a_scratch, scope="borrow")
    assert _read(report_a, "rates") == [("a", 2)]
    assert _read(report_a, "scratch") == [("a-scratch",)]
    with raises(ProgrammingError):
        report_a.execute(text("select * from missing_table"))
    report_a.close()

    report_b = engine.connect()
    assert _read(report_b, "rates") == [("admin", 1)]
    _assert_missing(report_b, "scratch")

    register_dataframe(report_b, "scratch", report_b_scratch, scope="borrow")
    assert _read(report_b, "scratch") == [("b-scratch",)]
    report_b.close()

    with engine.connect() as conn:
        assert _read(conn, "rates") == [("admin", 1)]
        _assert_missing(conn, "scratch")


def test_borrow_scope_via_execution_options() -> None:
    engine = create_engine("duckdb:///:memory:")
    temporary = _frame({"a": ["temporary"]})

    with engine.connect() as conn:
        conn.exec_driver_sql(
            "register",
            ("scratch", temporary),
            execution_options={"duckdb_register_scope": "borrow"},
        )
        assert _read(conn, "scratch") == [("temporary",)]

    with engine.connect() as conn:
        _assert_missing(conn, "scratch")


def test_borrow_scope_via_statement_execution_options() -> None:
    engine = create_engine("duckdb:///:memory:")
    temporary = _frame({"a": ["temporary"]})

    with engine.connect() as conn:
        statement = text("register(:name, :df)").execution_options(
            duckdb_register_scope="borrow"
        )
        conn.execute(statement, {"name": "scratch", "df": temporary})
        assert _read(conn, "scratch") == [("temporary",)]

    with engine.connect() as conn:
        _assert_missing(conn, "scratch")


def test_execution_option_scope_does_not_leak_to_next_borrow() -> None:
    engine = create_engine("duckdb:///:memory:")
    temporary = _frame({"a": ["temporary"]})
    shared = _frame({"a": ["shared"]})

    with engine.connect().execution_options(duckdb_register_scope="borrow") as conn:
        conn.exec_driver_sql("register", ("scratch", temporary))

    with engine.connect() as conn:
        _assert_missing(conn, "scratch")
        register_dataframe(conn, "scratch", shared)
        assert _read(conn, "scratch") == [("shared",)]


def test_unregister_borrow_scope_restores_shared_registration() -> None:
    engine = create_engine("duckdb:///:memory:")
    shared = _frame({"a": ["shared"]})
    temporary = _frame({"a": ["temporary"]})

    with engine.connect() as conn:
        register_dataframe(conn, "rates", shared)
        register_dataframe(conn, "rates", temporary, scope="borrow")
        assert _read(conn, "rates") == [("temporary",)]

        conn.exec_driver_sql(
            "unregister",
            ("rates",),
            execution_options={"duckdb_register_scope": "borrow"},
        )
        assert _read(conn, "rates") == [("shared",)]

    with engine.connect() as conn:
        assert _read(conn, "rates") == [("shared",)]


duckdb_version = duckdb.__version__


@mark.remote_data
@mark.skipif(
    "dev" in duckdb_version, reason="md extension not available for dev builds"
)
@mark.skipif(
    duckdb_version != "1.1.1", reason="md extension not available for this version"
)
def test_motherduck() -> None:
    engine = create_engine(
        "duckdb:///md:motherdb",
        connect_args={"config": {"motherduck_token": "motherduckdb_token"}},
    )

    with raises(
        ProgrammingError,
        match="Jwt is not in the form of Header.Payload.Signature with two dots and 3 sections",
    ):
        engine.connect()
