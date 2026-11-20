import urllib.parse

import pytest
from bs4 import BeautifulSoup as Soup

from datasette.app import Datasette
from datasette.database import Database
from datasette.facets import DateFacet
from datasette.utils.asgi import Request
from datasette.utils.timezones import (
    EMPTY_DATE,
    INVALID_DATE,
    timezone_date,
)


def test_timezone_date_handles_fall_back_boundary():
    # 2026-11-01 is the US fall-back day in New York.
    assert timezone_date("2026-11-01T00:15:00Z", "America/New_York") == "2026-10-31"
    # Both of these are 01:30 local, one before and one after the fall back.
    assert timezone_date("2026-11-01T05:30:00Z", "America/New_York") == "2026-11-01"
    assert timezone_date("2026-11-01T06:30:00Z", "America/New_York") == "2026-11-01"
    # In UTC the same instants are all on 2026-11-01.
    assert timezone_date("2026-11-01T00:15:00Z", "UTC") == "2026-11-01"
    assert timezone_date("2026-11-01T05:30:00Z", "UTC") == "2026-11-01"
    assert timezone_date("2026-11-01T06:30:00Z", "UTC") == "2026-11-01"


@pytest.mark.parametrize(
    "value",
    (
        None,
        "",
        "   ",
    ),
)
def test_timezone_date_empty_values(value):
    assert timezone_date(value, "America/New_York") is None


@pytest.mark.parametrize(
    "value",
    (
        "not a date",
        "2026-13-01T00:00:00Z",
        "2026-11-01T25:00:00Z",
    ),
)
def test_timezone_date_invalid_values(value):
    assert timezone_date(value, "America/New_York") == INVALID_DATE


def test_timezone_date_accepts_explicit_offsets():
    assert timezone_date("2026-11-01T00:30:00-04:00", "America/New_York") == "2026-11-01"
    assert timezone_date("2026-11-01T00:30:00+05:30", "America/New_York") == "2026-10-31"


def test_timezone_date_leaves_date_only_values_alone():
    assert timezone_date("2026-11-01", "America/New_York") == "2026-11-01"


async def _events_ds():
    ds = Datasette([], memory=True)
    db = ds.add_database(Database(ds, memory_name="timezone_events"))
    await db.execute_write(
        "create table events (id integer primary key, happened text, source text)"
    )
    rows = [
        (1, "2026-11-01T00:15:00Z", "a"),
        (2, "2026-11-01T05:30:00Z", "a"),
        (3, "2026-11-01T06:30:00Z", "a"),
        (4, "2026-11-01T05:30:00Z", "b"),
        (5, "", "a"),
        (6, "not a date", "a"),
        (7, None, "a"),
    ]
    for row in rows:
        await db.execute_write(
            "insert into events (id, happened, source) values (?, ?, ?)", row
        )
    return ds


def _facet_counts(facet):
    return {(row["value"], row["label"]): row["count"] for row in facet["results"]}


async def test_date_facet_uses_timezone_and_source_filter():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json"
            "?_facet_date=happened&source=a&_tz=America/New_York&_extra=count"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 6
        facet = data["facet_results"]["results"]["happened"]
        counts = _facet_counts(facet)
        # 00:15Z is 2026-10-31 in New York, the two 01:30 local rows are on
        # 2026-11-01, and empty/unreadable values get their own buckets.
        assert counts[("2026-10-31", "2026-10-31")] == 1
        assert counts[("2026-11-01", "2026-11-01")] == 2
        assert counts[(EMPTY_DATE, "empty")] == 2
        assert counts[(INVALID_DATE, "unreadable")] == 1
        # The duplicate from source b is excluded by the source filter.
        assert sum(row["count"] for row in facet["results"]) == 6
    finally:
        ds.close()


async def test_date_facet_counts_match_opened_rows_and_timezone_switch():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json"
            "?_facet_date=happened&source=a&_tz=America/New_York"
        )
        results = response.json()["facet_results"]["results"]["happened"]["results"]
        for row in results:
            if row["value"] in (EMPTY_DATE, INVALID_DATE):
                continue
            opened = await ds.client.get(
                row["toggle_url"].replace("http://localhost", "") + "&_extra=count"
            )
            assert opened.status_code == 200
            assert opened.json()["count"] == row["count"]
            assert len(opened.json()["rows"]) == row["count"]

        # UTC assigns 00:15Z, 05:30Z and 06:30Z to the same date.
        utc = await ds.client.get(
            "/timezone_events/events.json"
            "?_facet_date=happened&source=a&_tz=UTC"
        )
        utc_results = utc.json()["facet_results"]["results"]["happened"]["results"]
        assert (
            next(row["count"] for row in utc_results if row["value"] == "2026-11-01")
            == 3
        )
        assert not any(row["value"] == "2026-10-31" for row in utc_results)
    finally:
        ds.close()


async def test_date_facet_links_preserve_timezone_and_source():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json"
            "?_facet_date=happened&source=a&_tz=America/New_York"
        )
        results = response.json()["facet_results"]["results"]["happened"]["results"]
        november = next(row for row in results if row["value"] == "2026-11-01")
        assert "source=a" in november["toggle_url"]
        assert november["toggle_url"].count("_tz=") == 1
        assert "_tz=America%2FNew_York" in november["toggle_url"]

        # Opening the facet link and switching dates invalidates pagination.
        opened = await ds.client.get(
            november["toggle_url"].replace("http://localhost", "")
        )
        assert opened.status_code == 200
        rows = opened.json()["rows"]
        assert {row["id"] for row in rows} == {2, 3}
    finally:
        ds.close()


async def test_switching_date_candidate_replaces_previous_date():
    ds = await _events_ds()
    try:
        # The facet SQL here is unfiltered, as it is when a facet is refreshed
        # for a still-selected date; the request already carries the old date.
        facet = DateFacet(
            ds,
            Request.fake(
                "/?happened__date=2026-10-31&_facet_date=happened"
                "&source=a&_tz=America/New_York"
            ),
            database="timezone_events",
            sql="select * from events where source = 'a' order by id",
            table="events",
        )
        buckets, _ = await facet.facet_results()
        results = buckets[0]["results"]
        october = next(row for row in results if row["value"] == "2026-10-31")
        november = next(row for row in results if row["value"] == "2026-11-01")
        assert october["selected"] is True
        assert november["selected"] is False
        # Switching candidates replaces the previous date instead of ANDing
        # two __date filters together and returning no rows.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(november["toggle_url"]).query)
        assert query["happened__date"] == ["2026-11-01"]
        assert query["source"] == ["a"]
        assert query["_tz"] == ["America/New_York"]
    finally:
        ds.close()


async def test_date_filter_matches_facet_semantics():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json"
            "?happened__date=2026-11-01&source=a&_tz=America/New_York"
        )
        assert response.status_code == 200
        assert {row["id"] for row in response.json()["rows"]} == {2, 3}

        utc = await ds.client.get(
            "/timezone_events/events.json"
            "?happened__date=2026-11-01&source=a&_tz=UTC"
        )
        assert {row["id"] for row in utc.json()["rows"]} == {1, 2, 3}
    finally:
        ds.close()


async def test_date_facet_empty_and_invalid_filters():
    ds = await _events_ds()
    try:
        empty = await ds.client.get(
            "/timezone_events/events.json"
            "?happened__date=empty&_tz=America/New_York"
        )
        assert {row["id"] for row in empty.json()["rows"]} == {5, 7}
        invalid = await ds.client.get(
            "/timezone_events/events.json"
            "?happened__date=invalid&_tz=America/New_York"
        )
        assert {row["id"] for row in invalid.json()["rows"]} == {6}
    finally:
        ds.close()


async def test_date_facet_changes_reset_pagination():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json"
            "?_size=1&_facet_date=happened&source=a&_tz=America/New_York"
        )
        data = response.json()
        assert data["next_url"]
        results = data["facet_results"]["results"]["happened"]["results"]
        november = next(row for row in results if row["value"] == "2026-11-01")
        assert "_next" not in november["toggle_url"]
    finally:
        ds.close()


async def test_date_facet_legacy_without_timezone():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json?_facet_date=happened&source=a"
        )
        assert response.status_code == 200
        results = response.json()["facet_results"]["results"]["happened"]["results"]
        # Legacy behavior uses SQLite date(), which keeps the 00:15Z row on
        # 2026-11-01 and omits null/unreadable values entirely.
        assert {(row["value"], row["count"]) for row in results} == {
            ("2026-11-01", 3)
        }
    finally:
        ds.close()


async def test_invalid_timezone_is_a_400():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json?_facet_date=happened&_tz=Not_A_Zone"
        )
        assert response.status_code == 400
        assert "Invalid timezone" in response.json()["error"]
    finally:
        ds.close()


async def test_timezone_from_table_configuration():
    ds = Datasette(
        [],
        memory=True,
        config={
            "databases": {
                "timezone_events": {
                    "tables": {
                        "events": {
                            "timezone": "America/New_York",
                            "facets": [
                                {"date": {"column": "happened"}},
                            ],
                        }
                    }
                }
            }
        },
    )
    db = ds.add_database(
        Database(ds, memory_name="timezone_events_configured"), name="timezone_events"
    )
    await db.execute_write(
        "create table events (id integer primary key, happened text, source text)"
    )
    for row in (
        (1, "2026-11-01T00:15:00Z", "a"),
        (2, "2026-11-01T05:30:00Z", "a"),
    ):
        await db.execute_write(
            "insert into events (id, happened, source) values (?, ?, ?)", row
        )
    try:
        response = await ds.client.get(
            "/timezone_events/events.json?source=a&_extra=facet_results"
        )
        results = response.json()["facet_results"]["results"]["happened"]["results"]
        assert {(row["value"], row["count"]) for row in results} == {
            ("2026-10-31", 1),
            ("2026-11-01", 1),
        }
        # An explicit query string timezone overrides the configuration.
        utc = await ds.client.get(
            "/timezone_events/events.json?source=a&_tz=UTC&_extra=facet_results"
        )
        utc_results = utc.json()["facet_results"]["results"]["happened"]["results"]
        assert {(row["value"], row["count"]) for row in utc_results} == {
            ("2026-11-01", 2),
        }

        # Opening the facet value's __date filter uses the same configured
        # facet timezone, not UTC.
        opened = await ds.client.get(
            "/timezone_events/events.json?happened__date=2026-10-31&source=a"
        )
        assert {row["id"] for row in opened.json()["rows"]} == {1}
    finally:
        ds.close()


async def test_timezone_state_links_are_not_duplicated():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events?_size=1&_facet_date=happened&source=a"
            "&_tz=America/New_York"
        )
        soup = Soup(response.text, "html.parser")
        links = [a["href"] for a in soup.select(".facet-info a, a.filters-clear")]
        assert links
        for link in links:
            assert link.count("_tz=") <= 1
    finally:
        ds.close()


async def test_switching_timezone_ignores_old_cursor():
    ds = await _events_ds()
    try:
        response = await ds.client.get(
            "/timezone_events/events.json?_size=1&_tz=America/New_York"
        )
        next_url = response.json()["next_url"]
        assert "_tz_cursor=_tz%3DAmerica%2FNew_York" in next_url
        page_two = await ds.client.get(next_url.replace("http://localhost", ""))
        assert page_two.status_code == 200
        assert page_two.json()["rows"]

        # Reuse the same cursor with a different timezone: Datasette starts
        # again from the first page instead of skipping rows.
        switched = await ds.client.get(
            next_url.replace("http://localhost", "").replace(
                "_tz=America%2FNew_York", "_tz=UTC"
            )
        )
        assert switched.status_code == 200
        first = await ds.client.get("/timezone_events/events.json?_size=1&_tz=UTC")
        assert switched.json()["rows"] == first.json()["rows"]
    finally:
        ds.close()
