import csv
import io

import pytest

from datasette.app import Datasette
from datasette.database import QueryInterrupted

HEADINGS = [
    "id",
    "order_warehouse",
    "order_warehouse_label",
    "delivery_warehouse",
    "delivery_warehouse_label",
    "city",
    "city_label",
]


async def _make_ds(config=None):
    ds = Datasette(memory=True, config=config)
    db = ds.add_memory_database("data")
    await db.execute_write_script("""
        create table warehouses (id integer primary key, name text);
        create table cities (id integer primary key, name text);
        create table orders (
            id integer primary key,
            order_warehouse integer references warehouses(id),
            delivery_warehouse integer references warehouses(id),
            city integer references cities(id)
        );
        insert into warehouses (id, name) values (7, 'Warehouse Seven');
        insert into cities (id, name) values (7, 'City Seven');
    """)
    await db.execute_write_script(
        "insert into orders (id, order_warehouse, delivery_warehouse, city) "
        "values " + ", ".join(f"({i}, 7, 7, 7)" for i in range(1, 26))
    )
    await ds.invoke_startup()
    return ds


def _rows(response):
    return list(csv.DictReader(io.StringIO(response.text)))


def _count_label_queries(db, monkeypatch):
    counts = {"warehouses": 0, "cities": 0, "warehouse_sevens": 0}
    original_execute = db.execute

    async def counting_execute(sql, params=None, *args, **kwargs):
        params = params or []
        if "from warehouses" in sql:
            counts["warehouses"] += 1
            counts["warehouse_sevens"] += list(params).count(7)
        elif "from cities" in sql:
            counts["cities"] += 1
        return await original_execute(sql, params, *args, **kwargs)

    monkeypatch.setattr(db, "execute", counting_execute)
    return counts


async def test_streaming_export_reuses_warehouse_label_lookup(monkeypatch):
    ds = await _make_ds()
    counts = _count_label_queries(ds.get_database("data"), monkeypatch)
    try:
        response = await ds.client.get(
            "/data/orders.csv?_stream=1&_labels=on&_size=10"
        )
        assert response.status_code == 200
        assert response.text.splitlines()[0].split(",") == HEADINGS
        rows = _rows(response)
        assert len(rows) == 25
        for row in rows:
            assert row["order_warehouse_label"] == "Warehouse Seven"
            assert row["delivery_warehouse_label"] == "Warehouse Seven"
            assert row["city_label"] == "City Seven"
        # Three pages, but only one query each against warehouses and cities.
        # Both warehouse columns point at the same target, so they share it.
        assert counts == {
            "warehouses": 1,
            "cities": 1,
            "warehouse_sevens": 1,
        }
    finally:
        ds.close()


@pytest.mark.parametrize("size", (8, 13, 50))
async def test_page_size_does_not_change_column_mapping(monkeypatch, size):
    ds = await _make_ds()
    counts = _count_label_queries(ds.get_database("data"), monkeypatch)
    try:
        response = await ds.client.get(
            f"/data/orders.csv?_stream=1&_labels=on&_size={size}"
        )
        assert response.status_code == 200
        assert response.text.splitlines()[0].split(",") == HEADINGS
        rows = _rows(response)
        assert len(rows) == 25
        for row in rows:
            assert row["order_warehouse"] == "7"
            assert row["order_warehouse_label"] == "Warehouse Seven"
            assert row["delivery_warehouse"] == "7"
            assert row["delivery_warehouse_label"] == "Warehouse Seven"
            assert row["city"] == "7"
            assert row["city_label"] == "City Seven"
        # Each new download has its own cache, so it looks labels up again
        assert counts == {
            "warehouses": 1,
            "cities": 1,
            "warehouse_sevens": 1,
        }
    finally:
        ds.close()


async def test_label_timeout_is_not_cached_as_missing(monkeypatch):
    ds = await _make_ds()
    db = ds.get_database("data")
    counts = _count_label_queries(db, monkeypatch)
    original_execute = db.execute
    warehouse_attempts = []
    failed_once = False

    async def timeout_first_warehouse_query(sql, params=None, *args, **kwargs):
        nonlocal failed_once
        if "from warehouses" in sql:
            warehouse_attempts.append(list(params or []))
            if not failed_once:
                failed_once = True
                raise QueryInterrupted(Exception("interrupted"), sql, params)
        return await original_execute(sql, params, *args, **kwargs)

    monkeypatch.setattr(db, "execute", timeout_first_warehouse_query)
    try:
        response = await ds.client.get(
            "/data/orders.csv?_stream=1&_labels=on&_size=10"
        )
        assert response.status_code == 200
        rows = _rows(response)
        assert len(rows) == 25
        # The timed-out page keeps the existing shape: value only, blank label
        for row in rows[:10]:
            assert row["order_warehouse"] == "7"
            assert row["order_warehouse_label"] == ""
            assert row["delivery_warehouse_label"] == ""
        # Later pages get the real labels once the query succeeds
        for row in rows[10:]:
            assert row["order_warehouse_label"] == "Warehouse Seven"
            assert row["delivery_warehouse_label"] == "Warehouse Seven"
            assert row["city_label"] == "City Seven"
        # The timed-out attempt was retried with the same key on a later page
        assert warehouse_attempts == [[7], [7]]
        assert counts["warehouses"] == 1
        assert counts["cities"] == 1
    finally:
        ds.close()


async def test_new_download_uses_its_own_permissions(monkeypatch):
    ds = await _make_ds(
        config={
            "databases": {
                "data": {"tables": {"warehouses": {"allow": {"id": "alice"}}}}
            }
        }
    )
    db = ds.get_database("data")
    try:
        counts = _count_label_queries(db, monkeypatch)
        allowed = await ds.client.get(
            "/data/orders.csv?_stream=1&_labels=on&_size=10",
            cookies={"ds_actor": ds.client.actor_cookie({"id": "alice"})},
        )
        assert allowed.status_code == 200
        allowed_rows = _rows(allowed)
        assert allowed_rows[0]["order_warehouse_label"] == "Warehouse Seven"
        assert counts == {"warehouses": 1, "cities": 1, "warehouse_sevens": 1}

        counts = _count_label_queries(db, monkeypatch)
        anonymous = await ds.client.get(
            "/data/orders.csv?_stream=1&_labels=on&_size=10"
        )
        assert anonymous.status_code == 200
        anonymous_rows = _rows(anonymous)
        assert len(anonymous_rows) == 25
        # The earlier download's permission and labels are not reused
        for row in anonymous_rows:
            assert row["order_warehouse"] == "7"
            assert row["order_warehouse_label"] == ""
            assert row["delivery_warehouse_label"] == ""
            assert row["city_label"] == "City Seven"
        assert counts == {"warehouses": 0, "cities": 1, "warehouse_sevens": 0}
    finally:
        ds.close()


async def test_missing_target_rows_keep_existing_shape(monkeypatch):
    ds = Datasette(memory=True)
    db = ds.add_memory_database("data")
    await db.execute_write_script("""
        create table warehouses (id integer primary key, name text);
        create table orders (
            id integer primary key,
            order_warehouse integer references warehouses(id)
        );
        insert into warehouses (id, name) values (7, 'Warehouse Seven');
        insert into orders (id, order_warehouse) values (1, 999), (2, 7);
    """)
    await ds.invoke_startup()
    try:
        response = await ds.client.get(
            "/data/orders.csv?_stream=1&_labels=on&_size=10"
        )
        assert response.status_code == 200
        assert response.text.splitlines()[0].split(",") == [
            "id",
            "order_warehouse",
            "order_warehouse_label",
        ]
        rows = _rows(response)
        assert rows == [
            {"id": "1", "order_warehouse": "999", "order_warehouse_label": ""},
            {
                "id": "2",
                "order_warehouse": "7",
                "order_warehouse_label": "Warehouse Seven",
            },
        ]
    finally:
        ds.close()
