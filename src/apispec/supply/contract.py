"""APISpec contract for the supply interface.

Both inputs of the shared schema family are registered on one specification:
one component per record format (its nested item/detail schemas are
auto-registered alongside it). Components are registered by explicit stable
names so the generated document reads real wire keys (``data_key``) rather
than Python attribute names.

Registering only a single request or response component on a spec keeps
working exactly like plain ``spec.components.schema`` calls.
"""

from __future__ import annotations

from apispec import APISpec
from apispec.ext.marshmallow import MarshmallowPlugin

from .schemas import SupplyRecordV1Schema, SupplyRecordV2Schema

SPEC_TITLE = "Supply Interface"
SPEC_VERSION = "1.0.0"
OPENAPI_VERSION = "3.0.0"

# Component names used in the generated document.
RECORD_V1 = "SupplyRecordV1"
RECORD_V2 = "SupplyRecordV2"


def build_spec() -> APISpec:
    """Build the APISpec with both supply versions registered.

    Registration order is irrelevant: components are addressed by stable names
    and nested schemas are referenced, never inlined into another version.
    """
    spec = APISpec(
        title=SPEC_TITLE,
        version=SPEC_VERSION,
        openapi_version=OPENAPI_VERSION,
        plugins=(MarshmallowPlugin(),),
    )
    register_supply_schemas(spec)
    return spec


def register_supply_schemas(spec: APISpec) -> APISpec:
    """Register the V1 and V2 record components on ``spec``."""
    spec.components.schema(RECORD_V1, schema=SupplyRecordV1Schema)
    spec.components.schema(RECORD_V2, schema=SupplyRecordV2Schema)
    return spec


__all__ = [
    "OPENAPI_VERSION",
    "RECORD_V1",
    "RECORD_V2",
    "SPEC_TITLE",
    "SPEC_VERSION",
    "SupplyRecordV1Schema",
    "SupplyRecordV2Schema",
    "build_spec",
    "register_supply_schemas",
]
