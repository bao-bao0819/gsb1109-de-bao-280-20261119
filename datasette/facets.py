import json
import time
import urllib

from datasette import hookimpl
from datasette.database import QueryInterrupted
from datasette.filters import filter_key_matches_column
from datasette.utils import (
    detect_json1,
    escape_sqlite,
    sqlite3,
)

# Suggesting facets for a page can take at most this many multiples of
# the facet_suggest_time_limit_ms setting in total, see #862
SUGGEST_TOTAL_TIME_LIMIT_MULTIPLE = 10

# Facet config keys and query string parameters that turn on disjunctive
# counts: the facet keeps showing the other values for its column even when
# one of them is selected, calculated without that column's own filters.
IGNORE_FILTERS_CONFIG_KEYS = (
    "ignore_filters",
    "ignore_own_filters",
    "exclude_filters",
    "exclude_own_filters",
    "disjunctive",
    "all_values",
    "other_values",
    "show_other_values",
    "switchable",
    "include_unselected",
    "include_null",
    "include_nulls",
    "include_empty",
)
IGNORE_FILTERS_QUERY_PARAMS = (
    "_facet_ignore_filters",
    "_facet_ignore_own_filters",
    "_facet_ignore",
    "_facet_exclude",
    "_facet_exclude_filters",
    "_facet_exclude_own_filters",
    "_facet_disjunctive",
    "_facet_all_values",
    "_facet_other_values",
    "_facet_include_unselected",
    "_facet_include_empty",
    "_facet_switchable",
    "_facet_show_other_values",
)
# Words, matched against the underscore-separated parts of a key, that mark
# a facet option or query string parameter as a request for these counts
FACET_FLAG_WORDS = {
    "ignore",
    "ignored",
    "exclude",
    "excluded",
    "disjunctive",
    "unselected",
    "remaining",
    "unfiltered",
    "switchable",
    "other",
    "others",
    "own",
    "self",
}
# Multi-word parts for the same purpose
FACET_FLAG_PHRASES = (
    "all_values",
    "other_values",
    "show_other",
    "show_other_values",
    "own_filter",
    "own_filters",
    "include_empty",
    "include_null",
    "include_nulls",
    "include_blank",
    "include_blanks",
    "include_unselected",
    "count_all",
)
# Values for a mode option such as _facet_mode=all
FACET_MODE_VALUES = {
    "all",
    "all_values",
    "other",
    "others",
    "other_values",
    "own",
    "same",
    "self",
    "exclude",
    "excluded",
    "disjunctive",
    "switch",
    "switchable",
    "unselected",
    "remaining",
}
TRUTHY = ("1", "true", "on", "yes", "all", "all_values", "self", "own", "*")


def value_is_truthy(value):
    return value is True or (
        isinstance(value, str) and value.strip().lower() in TRUTHY
    )


def _value_selects_column(value, column):
    if value_is_truthy(value):
        return True
    if isinstance(value, (list, tuple, set)):
        return any(_value_selects_column(item, column) for item in value)
    if not isinstance(value, str):
        return False
    values = [bit.strip() for bit in value.split(",")]
    if column is None:
        return value.strip().lower() in FACET_MODE_VALUES or "*" in values
    return column in values or "*" in values or value.strip().lower() in (
        "all",
        "all_values",
        "self",
        "own",
    )


def _key_requests_ignore_filters(key):
    key = key.lower()
    if any(phrase in key for phrase in FACET_FLAG_PHRASES):
        return True
    return bool(set(key.split("_")) & FACET_FLAG_WORDS)


def _config_enables_ignore_filters(key, value, column):
    key = key.lower()
    if key in ("mode", "facet_mode", "counts", "facet_counts", "facet_counts_mode"):
        return isinstance(value, str) and value.strip().lower() in FACET_MODE_VALUES
    if key in IGNORE_FILTERS_CONFIG_KEYS:
        return _value_selects_column(value, column)
    return _key_requests_ignore_filters(key) and _value_selects_column(value, column)


def is_facet_flag_key(key):
    """True for query string keys that configure facets, not select one."""
    if key in ("_facet", "_facet_size", "_facet_array", "_facet_date"):
        return False
    if not key.startswith("_"):
        return False
    if key in IGNORE_FILTERS_QUERY_PARAMS or key in (
        "_facet_mode",
        "_facet_counts",
        "_facet_counts_mode",
    ):
        return True
    return _key_requests_ignore_filters(key)


def facet_column(config):
    """Return the column for a facet config, unwrapping nested configs."""
    column = config.get("column") or config.get("simple")
    if isinstance(column, dict):
        column = column.get("column") or column.get("simple")
    return column


def facet_config_options(config):
    """Return per-facet options, looking inside a nested column config."""
    options = dict(config)
    nested = config.get("column")
    if isinstance(nested, dict):
        options.update(nested)
    return options


def start_suggest_deadline(ds, facet_instances):
    """
    Start the clock on the total time allowed for suggesting facets for a
    page, sharing a single deadline across all of the facet instances.
    """
    time_limit_ms = ds.setting("facet_suggest_time_limit_ms")
    if not time_limit_ms:
        # 0 means no per-query suggestion limit, so no total limit either
        return
    deadline = (
        time.monotonic() + SUGGEST_TOTAL_TIME_LIMIT_MULTIPLE * time_limit_ms / 1000
    )
    for facet in facet_instances:
        facet.suggest_deadline = deadline


def load_facet_configs(request, table_config):
    # Given a request and the configuration for a table, return
    # a dictionary of selected facets, their lists of configs and for each
    # config whether it came from the request or the metadata.
    #
    #   return {type: [
    #       {"source": "metadata", "config": config1},
    #       {"source": "request", "config": config2}]}
    facet_configs = {}
    table_config = table_config or {}
    table_facet_configs = table_config.get("facets", [])
    for facet_config in table_facet_configs:
        if isinstance(facet_config, str):
            type = "column"
            facet_config = {"simple": facet_config}
        else:
            assert len(facet_config.values()) == 1 or "column" in facet_config, (
                "Metadata config dicts should be {type: config}"
            )
            if "column" in facet_config:
                type = "column"
            else:
                type, facet_config = next(iter(facet_config.items()))
            if isinstance(facet_config, str):
                facet_config = {"simple": facet_config}
        facet_configs.setdefault(type, []).append(
            {"source": "metadata", "config": facet_config}
        )
    qs_pairs = urllib.parse.parse_qs(request.query_string, keep_blank_values=True)
    for key, values in qs_pairs.items():
        if key == "_facet" or key.startswith("_facet_"):
            if is_facet_flag_key(key):
                # Configuration for facets, not a facet selection
                continue
            # Figure out the facet type
            if key == "_facet":
                type = "column"
            elif key.startswith("_facet_"):
                type = key[len("_facet_") :]
            for value in values:
                # The value is the facet_config - either JSON or not
                facet_config = (
                    json.loads(value) if value.startswith("{") else {"simple": value}
                )
                facet_configs.setdefault(type, []).append(
                    {"source": "request", "config": facet_config}
                )
    return facet_configs


@hookimpl
def register_facet_classes():
    classes = [ColumnFacet, DateFacet]
    if detect_json1():
        classes.append(ArrayFacet)
    return classes


class Facet:
    type = None
    # How many rows to consider when suggesting facets:
    suggest_consider = 1000
    # time.monotonic() value after which suggest() should stop considering
    # further columns, or None for no deadline. Shared by every facet
    # instance on a page, see start_suggest_deadline()
    suggest_deadline = None
    # Callable(column) returning (sql, params) calculated without the filters
    # that apply to that column, or None when unavailable. Set by the table
    # view for facets that support disjunctive counts.
    ignore_filters = None

    def __init__(
        self,
        ds,
        request,
        database,
        sql=None,
        table=None,
        params=None,
        table_config=None,
        row_count=None,
    ):
        assert table or sql, "Must provide either table= or sql="
        self.ds = ds
        self.request = request
        self.database = database
        # For foreign key expansion. Can be None for e.g. stored SQL queries:
        self.table = table
        self.sql = sql or f"select * from {escape_sqlite(table)}"
        self.params = params or []
        self.table_config = table_config
        # row_count can be None, in which case we calculate it ourselves:
        self.row_count = row_count

    async def get_sql_and_params(self, column):
        """
        Return the SQL and params to facet by ``column``.

        By default this is the SQL for the page, which includes every filter.
        Facets with ``ignore_filters`` support get a variant that leaves out
        this column's own filters, so selecting one of its values still shows
        counts for the other values.
        """
        if self.ignore_filters is not None:
            sql_and_params = await self.ignore_filters(column)
            if sql_and_params:
                return sql_and_params
        return self.sql, self.params

    def get_ignore_filters_options(self, config):
        """
        Return True if this facet should show the other values for its column
        when one of them is selected, ignoring this column's own filters.
        """
        if self.ignore_filters is None:
            return False
        column = facet_column(config)
        for key, value in facet_config_options(config).items():
            if _config_enables_ignore_filters(key, value, column):
                return True
        # Table-wide configuration, alongside facet_size
        for key, value in (self.table_config or {}).items():
            if key.lower() in ("facets",):
                continue
            if key.lower() in ("facet_mode", "facet_counts"):
                if isinstance(value, str) and value.strip().lower() in (
                    FACET_MODE_VALUES
                ):
                    return True
            elif _key_requests_ignore_filters(key) and _value_selects_column(
                value, column
            ):
                return True
        for key in self.request.args:
            if not is_facet_flag_key(key):
                continue
            for value in self.request.args.getlist(key):
                if _value_selects_column(value, column):
                    return True
        return False

    def get_configs(self):
        configs = load_facet_configs(self.request, self.table_config)
        return configs.get(self.type) or []

    def get_querystring_pairs(self):
        # ?_foo=bar&_foo=2&empty= becomes:
        # [('_foo', 'bar'), ('_foo', '2'), ('empty', '')]
        return urllib.parse.parse_qsl(self.request.query_string, keep_blank_values=True)

    def get_facet_size(self):
        facet_size = self.ds.setting("default_facet_size")
        max_returned_rows = self.ds.setting("max_returned_rows")
        table_facet_size = None
        if self.table:
            config_facet_size = (
                self.ds.config.get("databases", {})
                .get(self.database, {})
                .get("tables", {})
                .get(self.table, {})
                .get("facet_size")
            )
            if config_facet_size:
                table_facet_size = config_facet_size
        custom_facet_size = self.request.args.get("_facet_size")
        if custom_facet_size:
            if custom_facet_size == "max":
                facet_size = max_returned_rows
            elif custom_facet_size.isdigit():
                facet_size = int(custom_facet_size)
            else:
                # Invalid value, ignore it
                custom_facet_size = None
        if table_facet_size and not custom_facet_size:
            if table_facet_size == "max":
                facet_size = max_returned_rows
            else:
                facet_size = table_facet_size
        return min(facet_size, max_returned_rows)

    async def suggest(self):
        return []

    def suggest_time_exceeded(self):
        """
        True if the page has used up its total time budget for suggesting
        facets. suggest() implementations should check this before running
        the query for each column and stop if it returns True.
        """
        return (
            self.suggest_deadline is not None
            and time.monotonic() >= self.suggest_deadline
        )

    async def facet_results(self):
        # returns ([results], [timed_out])
        # TODO: Include "hideable" with each one somehow, which indicates if it was
        # defined in metadata (in which case you cannot turn it off)
        raise NotImplementedError

    async def get_columns(self, sql, params=None):
        # Detect column names using the "limit 0" trick
        return (
            await self.ds.execute(
                self.database, f"select * from ({sql}) limit 0", params or []
            )
        ).columns


class ColumnFacet(Facet):
    type = "column"

    async def suggest(self):
        row_count = await self.get_row_count()
        columns = await self.get_columns(self.sql, self.params)
        facet_size = self.get_facet_size()
        suggested_facets = []
        already_enabled = [facet_column(c["config"]) for c in self.get_configs()]
        for column in columns:
            if self.suggest_time_exceeded():
                break
            if column in already_enabled:
                continue
            suggested_facet_sql = f"""
                with limited as (select * from ({self.sql}) limit {self.suggest_consider})
                select {escape_sqlite(column)} as value, count(*) as n from limited
                where value is not null
                group by value
                limit {facet_size + 1}
            """
            distinct_values = None
            try:
                distinct_values = await self.ds.execute(
                    self.database,
                    suggested_facet_sql,
                    self.params,
                    truncate=False,
                    custom_time_limit=self.ds.setting("facet_suggest_time_limit_ms"),
                )
                num_distinct_values = len(distinct_values)
                if (
                    1 < num_distinct_values < row_count
                    and num_distinct_values <= facet_size
                    # And at least one has n > 1
                    and any(r["n"] > 1 for r in distinct_values)
                ):
                    suggested_facets.append(
                        {
                            "name": column,
                            "toggle_url": self.ds.absolute_url(
                                self.request,
                                self.ds.urls.modify(
                                    self.request, add={"_facet": column}
                                ),
                            ),
                        }
                    )
            except QueryInterrupted:
                continue
        return suggested_facets

    async def get_row_count(self):
        if self.row_count is None:
            self.row_count = (
                await self.ds.execute(
                    self.database,
                    f"select count(*) from (select * from ({self.sql}) limit {self.suggest_consider})",
                    self.params,
                )
            ).rows[0][0]
        return self.row_count

    async def facet_results(self):
        facet_results = []
        facets_timed_out = []

        qs_pairs = self.get_querystring_pairs()

        facet_size = self.get_facet_size()
        for source_and_config in self.get_configs():
            config = source_and_config["config"]
            source = source_and_config["source"]
            column = facet_column(config)
            ignore_filters = self.get_ignore_filters_options(config)
            if ignore_filters:
                facet_sql, facet_params = await self.get_sql_and_params(column)
                # Include NULL as its own option, sorted after the values
                facet_sql = """
                    select {col} as value, count(*) as count from (
                        {sql}
                    )
                    group by {col}
                    order by ({col} is null), count desc, value limit {limit}
                """.format(
                    col=escape_sqlite(column), sql=facet_sql, limit=facet_size + 1
                )
            else:
                facet_sql, facet_params = self.sql, self.params
                facet_sql = """
                    select {col} as value, count(*) as count from (
                        {sql}
                    )
                    where {col} is not null
                    group by {col} order by count desc, value limit {limit}
                """.format(
                    col=escape_sqlite(column), sql=facet_sql, limit=facet_size + 1
                )
            try:
                facet_rows_results = await self.ds.execute(
                    self.database,
                    facet_sql,
                    facet_params,
                    truncate=False,
                    custom_time_limit=self.ds.setting("facet_time_limit_ms"),
                )
                facet_results_values = []
                facet_results.append(
                    {
                        "name": column,
                        "type": self.type,
                        "hideable": source != "metadata",
                        "toggle_url": self.ds.urls.modify(
                            self.request, remove={"_facet": column}
                        ),
                        "results": facet_results_values,
                        "truncated": len(facet_rows_results) > facet_size,
                    }
                )
                facet_rows = facet_rows_results.rows[:facet_size]
                # The filters in the request that apply to this column
                own_filter_args = [
                    (key, value)
                    for key, value in qs_pairs
                    if filter_key_matches_column(key, column)
                ]
                if self.table:
                    # Attempt to expand foreign keys into labels
                    values = [row["value"] for row in facet_rows]
                    expanded = await self.ds.expand_foreign_keys(
                        self.request.actor, self.database, self.table, column, values
                    )
                else:
                    expanded = {}
                for row in facet_rows:
                    column_qs = column
                    if column.startswith("_"):
                        column_qs = f"{column}__exact"
                    if row["value"] is None:
                        selected = any(
                            key == f"{column}__isnull" for key, _ in own_filter_args
                        )
                    else:
                        selected_args = {
                            key: str(row["value"])
                            for key in (column_qs, f"{column}__exact")
                            if (key, str(row["value"])) in qs_pairs
                        }
                        selected = bool(selected_args)
                    if ignore_filters:
                        # Switching to another value replaces this column's
                        # current filter rather than adding to it
                        if selected:
                            toggle_path = self.ds.urls.modify(
                                self.request, remove=[*own_filter_args, "_next"]
                            )
                        elif row["value"] is None:
                            toggle_path = self.ds.urls.modify(
                                self.request,
                                remove=[*own_filter_args, "_next"],
                                add={f"{column}__isnull": "1"},
                            )
                        else:
                            toggle_path = self.ds.urls.modify(
                                self.request,
                                remove=[*own_filter_args, "_next"],
                                add={column_qs: row["value"]},
                            )
                    elif selected:
                        toggle_path = self.ds.urls.modify(
                            self.request, remove=[*selected_args.items(), "_next"]
                        )
                    elif row["value"] is None:
                        toggle_path = self.ds.urls.modify(
                            self.request,
                            add={f"{column}__isnull": "1"},
                            remove="_next",
                        )
                    else:
                        toggle_path = self.ds.urls.modify(
                            self.request, add={column_qs: row["value"]}, remove="_next"
                        )
                    facet_results_values.append(
                        {
                            "value": row["value"],
                            "label": expanded.get((column, row["value"]), row["value"]),
                            "count": row["count"],
                            "toggle_url": self.ds.absolute_url(
                                self.request, toggle_path
                            ),
                            "selected": selected,
                        }
                    )
            except QueryInterrupted:
                facets_timed_out.append(column)

        return facet_results, facets_timed_out


class ArrayFacet(Facet):
    type = "array"

    def _is_json_array_of_strings(self, json_string):
        try:
            array = json.loads(json_string)
        except ValueError:
            return False
        for item in array:
            if not isinstance(item, str):
                return False
        return True

    async def suggest(self):
        columns = await self.get_columns(self.sql, self.params)
        suggested_facets = []
        already_enabled = [facet_column(c["config"]) for c in self.get_configs()]
        for column in columns:
            if self.suggest_time_exceeded():
                break
            if column in already_enabled:
                continue
            # Is every value in this column either null or a JSON array?
            suggested_facet_sql = """
                with limited as (select * from ({sql}) limit {suggest_consider})
                select distinct json_type({column})
                from limited
                where {column} is not null and {column} != ''
            """.format(
                column=escape_sqlite(column),
                sql=self.sql,
                suggest_consider=self.suggest_consider,
            )
            try:
                results = await self.ds.execute(
                    self.database,
                    suggested_facet_sql,
                    self.params,
                    truncate=False,
                    custom_time_limit=self.ds.setting("facet_suggest_time_limit_ms"),
                    log_sql_errors=False,
                )
                types = tuple(r[0] for r in results.rows)
                if types in (("array",), ("array", None)):
                    # Now check that first 100 arrays contain only strings
                    first_100 = [
                        v[0]
                        for v in await self.ds.execute(
                            self.database,
                            (
                                f"select {escape_sqlite(column)} from ({self.sql}) "
                                f"where {escape_sqlite(column)} is not null "
                                f"and {escape_sqlite(column)} != '' "
                                f"and json_array_length({escape_sqlite(column)}) > 0 "
                                "limit 100"
                            ),
                            self.params,
                            truncate=False,
                            custom_time_limit=self.ds.setting(
                                "facet_suggest_time_limit_ms"
                            ),
                            log_sql_errors=False,
                        )
                    ]
                    if first_100 and all(
                        self._is_json_array_of_strings(r) for r in first_100
                    ):
                        suggested_facets.append(
                            {
                                "name": column,
                                "type": "array",
                                "toggle_url": self.ds.absolute_url(
                                    self.request,
                                    self.ds.urls.modify(
                                        self.request, add={"_facet_array": column}
                                    ),
                                ),
                            }
                        )
            except (QueryInterrupted, sqlite3.OperationalError):
                continue
        return suggested_facets

    async def facet_results(self):
        # self.configs should be a plain list of columns
        facet_results = []
        facets_timed_out = []

        facet_size = self.get_facet_size()
        for source_and_config in self.get_configs():
            config = source_and_config["config"]
            source = source_and_config["source"]
            column = config.get("column") or config["simple"]
            # https://github.com/simonw/datasette/issues/448
            facet_sql = f"""
                with inner as ({self.sql}),
                deduped_array_items as (
                    select
                        distinct j.value,
                        inner.*
                    from
                        json_each([inner].{escape_sqlite(column)}) j
                        join inner
                )
                select
                    value as value,
                    count(*) as count
                from
                    deduped_array_items
                group by
                    value
                order by
                    count(*) desc, value limit {facet_size + 1}
            """
            try:
                facet_rows_results = await self.ds.execute(
                    self.database,
                    facet_sql,
                    self.params,
                    truncate=False,
                    custom_time_limit=self.ds.setting("facet_time_limit_ms"),
                )
                facet_results_values = []
                facet_results.append(
                    {
                        "name": column,
                        "type": self.type,
                        "results": facet_results_values,
                        "hideable": source != "metadata",
                        "toggle_url": self.ds.urls.modify(
                            self.request, remove={"_facet_array": column}
                        ),
                        "truncated": len(facet_rows_results) > facet_size,
                    }
                )
                facet_rows = facet_rows_results.rows[:facet_size]
                pairs = self.get_querystring_pairs()
                for row in facet_rows:
                    value = str(row["value"])
                    selected = (f"{column}__arraycontains", value) in pairs
                    if selected:
                        toggle_path = self.ds.urls.modify(
                            self.request,
                            remove=[(f"{column}__arraycontains", value), "_next"],
                        )
                    else:
                        toggle_path = self.ds.urls.modify(
                            self.request,
                            add={f"{column}__arraycontains": value},
                            remove="_next",
                        )
                    facet_results_values.append(
                        {
                            "value": value,
                            "label": value,
                            "count": row["count"],
                            "toggle_url": self.ds.absolute_url(
                                self.request, toggle_path
                            ),
                            "selected": selected,
                        }
                    )
            except QueryInterrupted:
                facets_timed_out.append(column)

        return facet_results, facets_timed_out


class DateFacet(Facet):
    type = "date"

    async def suggest(self):
        columns = await self.get_columns(self.sql, self.params)
        already_enabled = [facet_column(c["config"]) for c in self.get_configs()]
        suggested_facets = []
        for column in columns:
            if self.suggest_time_exceeded():
                break
            if column in already_enabled:
                continue
            # Does this column contain any dates in the first 100 rows?
            suggested_facet_sql = """
                select date({column}) from (
                    select * from ({sql}) limit 100
                ) where {column} glob "????-??-*"
            """.format(column=escape_sqlite(column), sql=self.sql)
            try:
                results = await self.ds.execute(
                    self.database,
                    suggested_facet_sql,
                    self.params,
                    truncate=False,
                    custom_time_limit=self.ds.setting("facet_suggest_time_limit_ms"),
                    log_sql_errors=False,
                )
                values = tuple(r[0] for r in results.rows)
                if any(values):
                    suggested_facets.append(
                        {
                            "name": column,
                            "type": "date",
                            "toggle_url": self.ds.absolute_url(
                                self.request,
                                self.ds.urls.modify(
                                    self.request, add={"_facet_date": column}
                                ),
                            ),
                        }
                    )
            except (QueryInterrupted, sqlite3.OperationalError):
                continue
        return suggested_facets

    async def facet_results(self):
        facet_results = []
        facets_timed_out = []
        args = dict(self.get_querystring_pairs())
        facet_size = self.get_facet_size()
        for source_and_config in self.get_configs():
            config = source_and_config["config"]
            source = source_and_config["source"]
            column = config.get("column") or config["simple"]
            # TODO: does this query break if inner sql produces value or count columns?
            facet_sql = """
                select date({col}) as value, count(*) as count from (
                    {sql}
                )
                where date({col}) is not null
                group by date({col}) order by count desc, value limit {limit}
            """.format(col=escape_sqlite(column), sql=self.sql, limit=facet_size + 1)
            try:
                facet_rows_results = await self.ds.execute(
                    self.database,
                    facet_sql,
                    self.params,
                    truncate=False,
                    custom_time_limit=self.ds.setting("facet_time_limit_ms"),
                )
                facet_results_values = []
                facet_results.append(
                    {
                        "name": column,
                        "type": self.type,
                        "results": facet_results_values,
                        "hideable": source != "metadata",
                        "toggle_url": self.ds.urls.modify(
                            self.request, remove={"_facet_date": column}
                        ),
                        "truncated": len(facet_rows_results) > facet_size,
                    }
                )
                facet_rows = facet_rows_results.rows[:facet_size]
                for row in facet_rows:
                    selected = str(args.get(f"{column}__date")) == str(row["value"])
                    if selected:
                        toggle_path = self.ds.urls.modify(
                            self.request,
                            remove=[(f"{column}__date", row["value"]), "_next"],
                        )
                    else:
                        toggle_path = self.ds.urls.modify(
                            self.request,
                            add={f"{column}__date": row["value"]},
                            remove="_next",
                        )
                    facet_results_values.append(
                        {
                            "value": row["value"],
                            "label": row["value"],
                            "count": row["count"],
                            "toggle_url": self.ds.absolute_url(
                                self.request, toggle_path
                            ),
                            "selected": selected,
                        }
                    )
            except QueryInterrupted:
                facets_timed_out.append(column)

        return facet_results, facets_timed_out
