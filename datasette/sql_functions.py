from datasette import hookimpl
from datasette.utils import escape_fts
from datasette.utils.timezones import register_timezone_sql_function


@hookimpl
def prepare_connection(conn):
    conn.create_function("escape_fts", 1, escape_fts)
    register_timezone_sql_function(conn)
