from typing import Any

from mashumaro.codecs.basic import BasicDecoder, BasicEncoder
from mashumaro.codecs.json import JSONDecoder, JSONEncoder

from finance.dialects import ArchiveDialect, SummaryDialect
from finance.models import Bill

__all__ = [
    "bill_from_archive_dict",
    "bill_from_archive_json",
    "bill_from_summary_dict",
    "bill_from_summary_json",
    "to_archive_dict",
    "to_archive_json",
    "to_summary_dict",
    "to_summary_json",
]

_archive_dict_encoder = BasicEncoder(Bill, default_dialect=ArchiveDialect)
_archive_dict_decoder = BasicDecoder(Bill, default_dialect=ArchiveDialect)
_archive_json_encoder = JSONEncoder(Bill, default_dialect=ArchiveDialect)
_archive_json_decoder = JSONDecoder(Bill, default_dialect=ArchiveDialect)
_summary_dict_encoder = BasicEncoder(Bill, default_dialect=SummaryDialect)
_summary_dict_decoder = BasicDecoder(Bill, default_dialect=SummaryDialect)
_summary_json_encoder = JSONEncoder(Bill, default_dialect=SummaryDialect)
_summary_json_decoder = JSONDecoder(Bill, default_dialect=SummaryDialect)


def to_archive_dict(bill: Bill) -> dict[str, Any]:
    return _archive_dict_encoder.encode(bill)


def bill_from_archive_dict(data: Any) -> Bill:
    return _archive_dict_decoder.decode(data)


def to_archive_json(bill: Bill) -> str:
    return _archive_json_encoder.encode(bill)


def bill_from_archive_json(data: str) -> Bill:
    return _archive_json_decoder.decode(data)


def to_summary_dict(bill: Bill) -> dict[str, Any]:
    return _summary_dict_encoder.encode(bill)


def bill_from_summary_dict(data: Any) -> Bill:
    return _summary_dict_decoder.decode(data)


def to_summary_json(bill: Bill) -> str:
    return _summary_json_encoder.encode(bill)


def bill_from_summary_json(data: str) -> Bill:
    return _summary_json_decoder.decode(data)
