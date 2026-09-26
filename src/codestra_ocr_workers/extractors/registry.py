"""Document extractor registry keyed by (country, document_type)."""

from __future__ import annotations

from ..errors import UnsupportedDocumentError
from .base import Extractor
from .do_driver_license import DominicanDriverLicenseExtractor

_ALL: tuple[Extractor, ...] = (DominicanDriverLicenseExtractor(),)
_EXTRACTORS: dict[tuple[str, str], Extractor] = {
    (ex.country, name): ex for ex in _ALL for name in (ex.document_type, *ex.document_type_aliases)
}


def get_extractor(country: str, document_type: str, schema_version: str | None) -> Extractor:
    """Resolve an extractor; ``schema_version=None`` selects the latest supported one."""
    extractor = _EXTRACTORS.get((country.upper(), document_type.lower()))
    if extractor is None:
        raise UnsupportedDocumentError(
            f"no extractor for country={country!r} document_type={document_type!r}"
        )
    if schema_version is not None and schema_version not in extractor.schema_versions:
        raise UnsupportedDocumentError(
            f"schema_version {schema_version!r} not supported; "
            f"supported: {', '.join(extractor.schema_versions)}",
            code="unsupported_schema_version",
        )
    return extractor


def all_extractors() -> list[Extractor]:
    return list(_ALL)
