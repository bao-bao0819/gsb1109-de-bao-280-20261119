"""Contract and routing tests for the versioned supply interface."""

from __future__ import annotations

import pytest
from marshmallow import ValidationError

from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin
from apispec.supply import (
    FORMAT_V1,
    FORMAT_V2,
    RECORD_V1,
    RECORD_V2,
    TABLE_NAMES,
    SupplyRecordV1Schema,
    SupplyRecordV2Schema,
    SupplyRouter,
    UnknownFormatError,
    build_spec,
    register_supply_schemas,
)

from .utils import build_ref, get_schemas, validate_spec

V1_RECORD = {
    "format": "v1",
    "items": [
        {"wire_key": "ART-001"},
        {"wire_key": "ART-002", "priority": 7},
    ],
    "details": {"tolerance": 3},
}

V2_RECORD = {
    "format": "v2",
    "items": [
        {"batch_number": 1001, "wire_key": "ART-101"},
        {"batch_number": 1002, "wire_key": "ART-102", "priority": 9},
    ],
    "details": {"tolerance": 8},
}


class TestContract:
    def test_both_versions_registered_on_one_spec(self):
        spec = build_spec()
        schemas = get_schemas(spec)
        assert RECORD_V1 in schemas
        assert RECORD_V2 in schemas
        # Nested item/detail schemas of each version are distinct components.
        assert "SupplyItemV1" in schemas
        assert "SupplyItemV2" in schemas
        assert "SupplyDetailsV1" in schemas
        assert "SupplyDetailsV2" in schemas

    def test_spec_validates_against_openapi(self):
        assert validate_spec(build_spec())

    def test_contract_uses_real_wire_keys_not_python_names(self):
        schemas = get_schemas(build_spec())
        v1_item_props = schemas["SupplyItemV1"]["properties"]
        v2_item_props = schemas["SupplyItemV2"]["properties"]
        assert "wire_key" in v1_item_props
        assert "article_code" not in v1_item_props
        assert "wire_key" in v2_item_props
        assert "article_code" not in v2_item_props

    def test_v1_item_is_string_article_under_wire_key(self):
        item = get_schemas(build_spec())["SupplyItemV1"]
        assert item["properties"]["wire_key"]["type"] == "string"
        assert item["required"] == ["wire_key"]
        assert "batch_number" not in item["properties"]

    def test_v2_item_requires_integer_batch_and_article(self):
        item = get_schemas(build_spec())["SupplyItemV2"]
        assert item["properties"]["batch_number"]["type"] == "integer"
        assert item["properties"]["wire_key"]["type"] == "string"
        assert item["required"] == ["batch_number", "wire_key"]

    def test_format_discriminator_is_version_scoped(self):
        schemas = get_schemas(build_spec())
        assert schemas[RECORD_V1]["properties"]["format"]["enum"] == ["v1"]
        assert schemas[RECORD_V2]["properties"]["format"]["enum"] == ["v2"]

    def test_details_is_nullable_and_versions_keep_own_defaults(self):
        schemas = get_schemas(build_spec())
        v1_details = schemas["SupplyDetailsV1"]["properties"]
        v2_details = schemas["SupplyDetailsV2"]["properties"]
        # Same-named field must not borrow the other version's default.
        assert v1_details["tolerance"]["default"] == 5
        assert v2_details["tolerance"]["default"] == 10
        assert v1_details["note"]["default"] == "v1-note"
        assert "note" not in v2_details
        for record in (RECORD_V1, RECORD_V2):
            details_prop = schemas[record]["properties"]["details"]
            assert details_prop["anyOf"][0]["nullable"] is True

    def test_item_defaults_are_not_shared_across_versions(self):
        schemas = get_schemas(build_spec())
        assert schemas["SupplyItemV1"]["properties"]["priority"]["default"] == 10
        assert schemas["SupplyItemV2"]["properties"]["priority"]["default"] == 20

    def test_nested_items_reference_their_own_version_components(self):
        spec = build_spec()
        schemas = get_schemas(spec)
        assert schemas[RECORD_V1]["properties"]["items"]["items"] == build_ref(
            spec, "schema", "SupplyItemV1"
        )
        assert schemas[RECORD_V2]["properties"]["items"]["items"] == build_ref(
            spec, "schema", "SupplyItemV2"
        )

    def test_registration_order_does_not_change_contract(self):
        forward = build_spec()

        reversed_spec = APISpec(
            title="Supply Interface",
            version="1.0.0",
            openapi_version="3.0.0",
            plugins=(MarshmallowPlugin(),),
        )
        reversed_spec.components.schema(RECORD_V2, schema=SupplyRecordV2Schema)
        reversed_spec.components.schema(RECORD_V1, schema=SupplyRecordV1Schema)

        assert get_schemas(forward) == get_schemas(reversed_spec)

    def test_single_component_registration_still_works(self):
        spec = APISpec(
            title="Single",
            version="1.0.0",
            openapi_version="3.0.0",
            plugins=(MarshmallowPlugin(),),
        )
        spec.components.schema(RECORD_V1, schema=SupplyRecordV1Schema)
        schemas = get_schemas(spec)
        assert RECORD_V1 in schemas
        assert RECORD_V2 not in schemas
        # The registered component can be wired as request or response body.
        spec.path(
            "/supply",
            operations={
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {"schema": SupplyRecordV1Schema}
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "Stored supply record",
                            "content": {
                                "application/json": {"schema": SupplyRecordV1Schema}
                            },
                        }
                    },
                }
            },
        )
        post = spec.to_dict()["paths"]["/supply"]["post"]
        ref = build_ref(spec, "schema", RECORD_V1)
        assert (
            post["requestBody"]["content"]["application/json"]["schema"] == ref
        )
        assert (
            post["responses"]["201"]["content"]["application/json"]["schema"]
            == ref
        )
        assert validate_spec(spec)

    def test_register_supply_schemas_is_idempotent_across_fresh_specs(self):
        spec = APISpec(
            title="Fresh",
            version="1.0.0",
            openapi_version="3.0.0",
            plugins=(MarshmallowPlugin(),),
        )
        register_supply_schemas(spec)
        assert set(get_schemas(spec)) == {
            RECORD_V1,
            RECORD_V2,
            "SupplyItemV1",
            "SupplyItemV2",
            "SupplyDetailsV1",
            "SupplyDetailsV2",
        }


class TestRouting:
    @pytest.mark.parametrize(
        ("payload", "version", "table"),
        [
            (V1_RECORD, FORMAT_V1, TABLE_NAMES[FORMAT_V1]),
            (V2_RECORD, FORMAT_V2, TABLE_NAMES[FORMAT_V2]),
        ],
    )
    def test_real_nested_records_validate_and_land_in_matching_table(
        self, payload, version, table
    ):
        router = SupplyRouter()
        row = router.route(payload)
        assert row.format == version
        assert row.table == table
        assert router.tables[version][0] is row
        assert router.tables[FORMAT_V1 if version == FORMAT_V2 else FORMAT_V2] == []

    def test_extracted_data_keeps_wire_representation(self):
        router = SupplyRouter()
        row = router.route(V1_RECORD)
        assert "wire_key" in row.wire["items"][0]
        assert "article_code" not in row.wire["items"][0]
        assert row.wire["items"][0]["wire_key"] == "ART-001"
        # Python-internal names only appear in the deserialized view.
        assert row.data["items"][0]["article_code"] == "ART-001"

    def test_v2_extracts_integer_batch_and_article(self):
        row = SupplyRouter().route(V2_RECORD)
        assert row.data["items"][0]["batch_number"] == 1001
        assert row.data["items"][0]["article_code"] == "ART-101"
        assert row.wire["items"][0]["batch_number"] == 1001

    def test_v2_declared_record_with_v1_article_payload_is_rejected(self):
        # format says v2 but the item only carries a V1-style string article.
        bad_record = {
            "format": "v2",
            "items": [{"wire_key": "ART-001"}],
        }
        router = SupplyRouter()
        with pytest.raises(ValidationError) as exc_info:
            router.route(bad_record)
        messages = exc_info.value.messages
        assert messages["items"][0]["batch_number"] == [
            "Missing data for required field."
        ]
        # Nothing may leak into either table.
        assert router.tables[FORMAT_V1] == []
        assert router.tables[FORMAT_V2] == []

    def test_v1_declared_record_rejects_v2_integer_batch_field(self):
        bad_record = {
            "format": "v1",
            "items": [{"wire_key": "ART-001", "batch_number": 1001}],
        }
        with pytest.raises(ValidationError) as exc_info:
            SupplyRouter().route(bad_record)
        assert "batch_number" in exc_info.value.messages["items"][0]

    def test_v1_string_article_rejects_integer_value(self):
        bad_record = {"format": "v1", "items": [{"wire_key": 1001}]}
        with pytest.raises(ValidationError) as exc_info:
            SupplyRouter().route(bad_record)
        assert exc_info.value.messages["items"][0]["wire_key"] == [
            "Not a valid string."
        ]

    def test_nested_required_error_points_to_exact_array_item(self):
        record = {
            "format": "v1",
            "items": [
                {"wire_key": "ART-001"},
                {},
                {"wire_key": "ART-003"},
            ],
        }
        with pytest.raises(ValidationError) as exc_info:
            SupplyRouter().route(record)
        messages = exc_info.value.messages
        assert 1 in messages["items"]
        assert messages["items"][1]["wire_key"] == [
            "Missing data for required field."
        ]
        assert 0 not in messages["items"]
        assert 2 not in messages["items"]

    def test_record_only_accepted_by_its_declared_version(self):
        # A V1 payload handed to the V2 declaration fails on the format
        # discriminator (and the V2-only required batch field).
        with pytest.raises(ValidationError) as exc_info:
            SupplyRecordV2Schema().load(V1_RECORD)
        assert "format" in exc_info.value.messages

        # A V2 payload handed to the V1 declaration is rejected too.
        with pytest.raises(ValidationError) as exc_info:
            SupplyRecordV1Schema().load(V2_RECORD)
        assert "format" in exc_info.value.messages

    def test_unknown_and_missing_format_raise_unknown_format(self):
        router = SupplyRouter()
        with pytest.raises(UnknownFormatError):
            router.route({"format": "v9", "items": []})
        with pytest.raises(UnknownFormatError):
            router.route({"items": []})

    @pytest.mark.parametrize("version", [FORMAT_V1, FORMAT_V2])
    def test_missing_details_applies_version_default(self, version):
        record = {"format": version, "items": []}
        data = SupplyRouter().validate(record)
        if version == FORMAT_V1:
            assert data["details"] == {"tolerance": 5, "note": "v1-note"}
        else:
            assert data["details"] == {"tolerance": 10}

    @pytest.mark.parametrize("version", [FORMAT_V1, FORMAT_V2])
    def test_explicit_null_details_stays_none(self, version):
        record = {"format": version, "items": [], "details": None}
        data = SupplyRouter().validate(record)
        assert data["details"] is None

    def test_defaults_are_not_borrowed_between_versions(self):
        v1 = SupplyRouter().validate({"format": "v1", "items": [{"wire_key": "A"}]})
        v2 = SupplyRouter().validate(
            {"format": "v2", "items": [{"batch_number": 1, "wire_key": "A"}]}
        )
        assert v1["items"][0]["priority"] == 10
        assert v2["items"][0]["priority"] == 20
        assert v1["details"]["tolerance"] == 5
        assert v2["details"]["tolerance"] == 10

    def test_routing_is_independent_of_component_registration_order(self):
        # Routing never consults the spec, so the order components are
        # registered cannot change which table a record lands in.
        spec_v1_first = APISpec(
            title="Supply Interface",
            version="1.0.0",
            openapi_version="3.0.0",
            plugins=(MarshmallowPlugin(),),
        )
        spec_v1_first.components.schema(RECORD_V1, schema=SupplyRecordV1Schema)
        spec_v1_first.components.schema(RECORD_V2, schema=SupplyRecordV2Schema)

        spec_v2_first = APISpec(
            title="Supply Interface",
            version="1.0.0",
            openapi_version="3.0.0",
            plugins=(MarshmallowPlugin(),),
        )
        spec_v2_first.components.schema(RECORD_V2, schema=SupplyRecordV2Schema)
        spec_v2_first.components.schema(RECORD_V1, schema=SupplyRecordV1Schema)

        for spec in (spec_v1_first, spec_v2_first):
            router = SupplyRouter()
            rows = [router.route(payload) for payload in (V2_RECORD, V1_RECORD)]
            assert [row.format for row in rows] == ["v2", "v1"]
            assert len(router.tables[FORMAT_V1]) == 1
            assert len(router.tables[FORMAT_V2]) == 1
            assert router.tables[FORMAT_V1][0].wire["items"][0]["wire_key"] == "ART-001"
            assert (
                router.tables[FORMAT_V2][0].wire["items"][0]["batch_number"] == 1001
            )

    def test_reversing_payload_order_keeps_tables_and_extraction_stable(self):
        router = SupplyRouter()
        router.route(V2_RECORD)
        router.route(V1_RECORD)

        reverse_router = SupplyRouter()
        reverse_router.route(V1_RECORD)
        reverse_router.route(V2_RECORD)

        for version in (FORMAT_V1, FORMAT_V2):
            assert (
                router.tables[version][0].wire
                == reverse_router.tables[version][0].wire
            )
            assert (
                router.tables[version][0].table
                == reverse_router.tables[version][0].table
            )

    def test_validate_runs_without_writing_any_table(self):
        router = SupplyRouter()
        result = router.validate(V1_RECORD)
        assert result["format"] == "v1"
        assert router.tables[FORMAT_V1] == []
        assert router.tables[FORMAT_V2] == []
