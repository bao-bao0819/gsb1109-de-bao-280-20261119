import copy

import pytest

from compat import contracts as c

SPEC_DIR = "compat/specs"


@pytest.fixture(scope="module")
def old():
    return c.load(f"{SPEC_DIR}/old.yaml")


@pytest.fixture(scope="module")
def renamed():
    return c.load(f"{SPEC_DIR}/renamed.yaml")


@pytest.fixture(scope="module")
def narrowed():
    return c.load(f"{SPEC_DIR}/narrowed-bulk.yaml")


@pytest.fixture(scope="module")
def tightened():
    return c.load(f"{SPEC_DIR}/tightened-request.yaml")


def operation(document):
    return c.operation(document, "/events", "post")[1]


def response(document):
    return c.response_schema(document, operation(document))


def location():
    return "paths./events.post"


def response_location():
    return "paths./events.post.responses.200"


def statuses(findings):
    return {finding.status for finding in findings}


BULK_BOTH = {"marker": "bulk", "count": 1, "items": [{"id": "node-1"}]}
BULK_COUNT = {"marker": "bulk", "count": 1}
SINGLE_ITEMS = {"marker": "single", "items": [{"id": "node-1"}]}

OLD_ONLY_REQUEST = {
    "header": {"X-Tenant": "tenant-a"},
    "body": {"kind": "single", "payload": "hello"},
}


def test_rename_and_reorder_is_not_a_data_break(old, renamed):
    request_findings = c.compare_request(
        old, renamed, operation(old), operation(renamed), location()
    )
    assert c.summarize(request_findings) is c.Status.EQUIVALENT

    response_findings = c.compare_response(
        old,
        renamed,
        response(old),
        response(renamed),
        response_location(),
        [BULK_COUNT, SINGLE_ITEMS],
    )
    assert c.summarize(response_findings) is c.Status.EQUIVALENT
    assert c.Status.BREAKING not in statuses(response_findings)


def test_rename_is_proved_through_local_refs_not_names(old, renamed):
    findings = c.compare_response(
        old,
        renamed,
        response(old),
        response(renamed),
        response_location(),
        [BULK_COUNT],
    )
    pairs = {
        (finding.old, finding.new)
        for finding in findings
        if finding.message.startswith("local reference pair")
    }
    assert (
        "#/components/schemas/EventNode",
        "#/components/schemas/RecordNode",
    ) in pairs
    assert (
        "#/components/schemas/EventEdge",
        "#/components/schemas/RecordEdge",
    ) in pairs


def test_bulk_sample_matches_two_old_branches(old, narrowed):
    findings = c.compare_response(
        old,
        narrowed,
        response(old),
        response(narrowed),
        response_location(),
        [BULK_BOTH],
    )
    sample = [f for f in findings if f.location.endswith(".samples[0]")]
    assert len(sample) == 1
    assert sample[0].status is c.Status.BREAKING
    assert sample[0].old == [0, 1]
    assert sample[0].new == [1]
    assert c.summarize(findings) is c.Status.BREAKING


def test_bulk_sample_is_not_routed_by_the_old_parser(old, narrowed):
    old_doc = old
    new_doc = narrowed
    old_branches = c._one_of(old_doc.resolve(response(old)), response_location())
    new_branches = c._one_of(new_doc.resolve(response(narrowed)), response_location())
    assert old_branches is not None and new_branches is not None

    old_matches, _ = c.match_branches(
        old_doc, old_branches, BULK_BOTH, "old"
    )
    new_matches, _ = c.match_branches(
        new_doc, new_branches, BULK_BOTH, "new"
    )
    assert old_matches == [0, 1]
    assert new_matches == [1]


def test_tightened_required_parameter_breaks_old_requests(old, tightened):
    findings = c.compare_request(
        old,
        tightened,
        operation(old),
        operation(tightened),
        location(),
        OLD_ONLY_REQUEST,
    )
    assert c.summarize(findings) is c.Status.BREAKING
    parameter = [
        f
        for f in findings
        if f.location == f"{location()}.parameters[query:region].required"
    ]
    assert len(parameter) == 1
    assert parameter[0].old is False
    assert parameter[0].new is True

    counterexample = [
        f for f in findings if f.location.endswith(".counterexample")
    ]
    assert len(counterexample) == 1
    assert counterexample[0].status is c.Status.BREAKING
    assert counterexample[0].old == []
    assert any("region" in item for item in counterexample[0].new)


def test_request_enum_narrowing_breaks_old_requests(old):
    new = copy.deepcopy(old.raw)
    new["paths"]["/events"]["post"]["parameters"][0]["schema"]["enum"] = ["us"]
    new_doc = c.Document(new)
    findings = c.compare_request(
        old, new_doc, operation(old), operation(new_doc), location()
    )
    enum = [
        f
        for f in findings
        if f.location == f"{location()}.parameters[query:region].schema.enum"
    ]
    assert len(enum) == 1
    assert enum[0].status is c.Status.BREAKING
    assert enum[0].old == ["us", "eu"]
    assert enum[0].new == ["us"]


def test_response_required_relaxation_is_unknown_not_silently_safe(old):
    new = copy.deepcopy(old.raw)
    new["components"]["schemas"]["BulkCountEvent"]["required"] = ["marker"]
    new_doc = c.Document(new)
    findings = c.compare_response(
        old,
        new_doc,
        response(old),
        response(new_doc),
        response_location(),
        [BULK_COUNT],
    )
    assert c.Status.BREAKING in statuses(findings)
    required = [
        f
        for f in findings
        if f.status is c.Status.BREAKING and f.location.endswith(".required")
    ]
    assert len(required) == 1
    assert required[0].old == ["count", "marker"]
    assert required[0].new == ["marker"]


def test_removed_old_request_field_is_unknown_scope(old):
    new = copy.deepcopy(old.raw)
    del new["components"]["schemas"]["EventRequest"]["properties"]["trace"]
    new_doc = c.Document(new)
    findings = c.compare_request(
        old, new_doc, operation(old), operation(new_doc), location()
    )
    assert any(
        f.status is c.Status.UNKNOWN and f.message.startswith("old request fields")
        for f in findings
    )


def test_changed_unmodelled_keyword_is_unknown(old):
    new = copy.deepcopy(old.raw)
    new["components"]["schemas"]["EventRequest"]["properties"]["payload"][
        "maxLength"
    ] = 1024
    new_doc = c.Document(new)
    findings = c.compare_request(
        old, new_doc, operation(old), operation(new_doc), location()
    )
    assert c.summarize(findings) is c.Status.UNKNOWN
    assert any(
        f.location.endswith("payload.maxLength")
        and f.status is c.Status.UNKNOWN
        for f in findings
    )


def test_single_item_sample_still_routes_safely_after_bulk_is_narrowed(old, narrowed):
    findings = c.compare_response(
        old,
        narrowed,
        response(old),
        response(narrowed),
        response_location(),
        [SINGLE_ITEMS],
    )
    sample = [f for f in findings if f.location.endswith(".samples[0]")]
    assert len(sample) == 1
    assert sample[0].status is c.Status.COMPATIBLE
    assert sample[0].old == "bulk_items"
    assert sample[0].new == "bulk_items"


def test_rename_with_changed_required_is_still_breaking(old):
    new = copy.deepcopy(old.raw)
    new["components"]["schemas"]["BulkCountEvent"]["required"] = ["marker"]
    new_doc = c.Document(new)
    findings = c.compare_response(
        old,
        new_doc,
        response(old),
        response(new_doc),
        response_location(),
        [BULK_COUNT],
    )
    assert c.Status.BREAKING in statuses(findings)


def test_missing_table_marker_is_unknown_even_when_samples_pass(old):
    new = copy.deepcopy(old.raw)
    for name in ("BulkCountEvent", "BulkItemsEvent"):
        del new["components"]["schemas"][name]["x-collector-table"]
    new_doc = c.Document(new)
    findings = c.compare_response(
        old,
        new_doc,
        response(old),
        response(new_doc),
        response_location(),
        [SINGLE_ITEMS],
    )
    assert c.summarize(findings) is c.Status.UNKNOWN
    assert any(
        f.status is c.Status.UNKNOWN and "table marker is missing" in f.message
        for f in findings
    )


def test_external_reference_is_unknown_scope(old):
    new = copy.deepcopy(old.raw)
    new["components"]["schemas"]["EventNode"]["properties"]["edge"] = {
        "$ref": "https://example.invalid/edge.yaml"
    }
    new_doc = c.Document(new)
    findings = c.compare_response(
        old,
        new_doc,
        response(old),
        response(new_doc),
        response_location(),
        [BULK_COUNT],
    )
    assert c.summarize(findings) is c.Status.UNKNOWN
    assert any(
        f.status is c.Status.UNKNOWN and "rename equivalence" in f.message
        for f in findings
    )


def test_circular_reference_is_terminated(old):
    document = copy.deepcopy(old.raw)
    findings = c.compare_request(
        old,
        c.Document(document),
        operation(old),
        operation(c.Document(document)),
        location(),
    )
    assert c.Status.BREAKING not in statuses(findings)


def test_one_of_branch_identity_uses_table_and_required_fields(old):
    branches = c._one_of(old.resolve(response(old)), response_location())
    assert branches is not None
    keys = {c._branch_key(old, branch) for branch in branches}
    assert ("bulk_count", ("count", "marker")) in keys
    assert ("bulk_items", ("items", "marker")) in keys
