"""Fixed material-catalogue snapshots for the group_by / ignore_order recon.

Business identity is defined as the pair (supplier, id); the numeric ``id``
alone is NOT unique because suppliers A and B both use id 7.

Every record is a plain dict with three fields:
    supplier: vendor code
    id:       vendor-local material id
    desc:     description text

The two vendors each have an id 7. Some imports emit those two rows in
swapped order. Vendor A additionally has id 8.
"""

T1 = [
    {"supplier": "A", "id": 7, "desc": "bolt M8 short"},
    {"supplier": "B", "id": 7, "desc": "gasket blue"},
    {"supplier": "A", "id": 8, "desc": "washer flat 10mm zinc"},
]

T2_SWAP = [
    {"supplier": "B", "id": 7, "desc": "gasket blue"},
    {"supplier": "A", "id": 7,
     "desc": "bolt M8 long and fully redesigned description"},
    {"supplier": "A", "id": 8, "desc": "washer flat 10mm zinc"},
]

T2_ORIG = [
    {"supplier": "A", "id": 7,
     "desc": "bolt M8 long and fully redesigned description"},
    {"supplier": "B", "id": 7, "desc": "gasket blue"},
    {"supplier": "A", "id": 8, "desc": "washer flat 10mm zinc"},
]

CONTROL_ID_CHANGE_T1 = [
    {"supplier": "A", "id": 8, "desc": "washer flat 10mm zinc"},
]
CONTROL_ID_CHANGE_T2 = [
    {"supplier": "A", "id": 9, "desc": "washer flat 10mm zinc"},
]

CONTROL_TINY_DESC_T1 = [
    {"supplier": "A", "id": 8, "desc": "washer flat 10mm zinc"},
]
CONTROL_TINY_DESC_T2 = [
    {"supplier": "A", "id": 8, "desc": "washer flat 10mm zinc plated"},
]

INNER_LIST_T1 = [
    {"supplier": "A", "id": 7, "batch": "Y",
     "desc": "keep this line untouched exactly"},
    {"supplier": "A", "id": 7, "batch": "X",
     "desc": "alpha bravo charlie delta echo foxtrot"},
    {"supplier": "B", "id": 7, "batch": "B1", "desc": "gasket blue"},
]
INNER_LIST_T2 = [
    {"supplier": "A", "id": 7, "batch": "X",
     "desc": "zulu yankee xray whiskey victor uniform tango sierra romeo quebec"},
    {"supplier": "A", "id": 7, "batch": "Y",
     "desc": "keep this line untouched exactly"},
    {"supplier": "B", "id": 7, "batch": "B1", "desc": "gasket blue"},
]

INTERSECTION_T1 = [
    {"supplier": "A", "id": 1, "desc": "row one"},
    {"supplier": "A", "id": 2, "desc": "row two"},
    {"supplier": "A", "id": 3, "desc": "row three stable"},
]
INTERSECTION_T2 = [
    {"supplier": "A", "id": 11, "desc": "row eleven"},
    {"supplier": "A", "id": 12, "desc": "row twelve"},
    {"supplier": "A", "id": 3, "desc": "row three stable"},
]


def business_key(record):
    return record["supplier"], record["id"]


LOSSY_GROUP_BY = "id"
FAITHFUL_GROUP_BY = business_key
FAITHFUL_SORT_KEY = "id"
INNER_SORT_KEY = "batch"
