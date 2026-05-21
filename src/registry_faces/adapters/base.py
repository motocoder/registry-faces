"""Base adapter interface.

Each source jurisdiction implements one adapter. The pipeline is:
  fetch()               -> iterator of raw dicts
  normalize(raw)        -> OffenderRecord (no photo metadata)
  extract_photos(raw)   -> list[PhotoRef]  (default: none)

Keep these phases distinct so that:
  * fetch() can be mocked / cached in tests
  * normalize() can be re-run against `record.raw` without re-fetching
  * extract_photos() can be re-run independently of either
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..photos import PhotoRef
from ..schema import OffenderRecord


class Adapter(ABC):
    jurisdiction: str = ""
    source_name: str = ""

    @abstractmethod
    def fetch(self) -> Iterator[dict]:
        """Yield raw records as dicts."""

    @abstractmethod
    def normalize(self, raw: dict) -> OffenderRecord:
        """Convert one raw record into the canonical schema."""

    def extract_photos(self, raw: dict) -> list[PhotoRef]:
        """Return any photo URLs published by the source for this person.

        Default returns an empty list. Override when the source publishes
        photo URLs. Only return URLs the source itself serves — never URLs
        discovered through search or other secondary lookup.
        """
        return []

    def run(self) -> Iterator[tuple[OffenderRecord, list[PhotoRef]]]:
        """Default pipeline: fetch -> (normalize, extract_photos) per record."""
        for raw in self.fetch():
            yield self.normalize(raw), self.extract_photos(raw)
