"""Contract for future log layouts, independent of storage and SQL dialect."""
from abc import ABC, abstractmethod


class LogFormat(ABC):
    @property
    @abstractmethod
    def name(self):
        """Stable configuration name for this source layout."""

    @property
    @abstractmethod
    def columns(self):
        """Ordered default source columns used for database projection."""

    @abstractmethod
    def validate_header(self, header):
        """Validate source headers according to this format's contract."""

    @abstractmethod
    def validate_projection(self, columns):
        """Validate ordered database columns selected for this source layout."""

    @abstractmethod
    def validate_row(self, row, source_record, header=None):
        """Validate one normalized source row without constructing an event."""

    @abstractmethod
    def to_event(self, row, source_record, header=None):
        """Normalize one source row to an immutable LogEvent."""
