"""Archive-specific ceilings in addition to the selected processing profile."""

from preprocessing.errors import ServiceError

ARCHIVE_OUTPUT_BYTES = 64 * 1024 * 1024
ARCHIVE_MANIFEST_BYTES = 384 * 1024
ARCHIVE_ENTRIES = 128
ARCHIVE_SCRATCH = "archive-scratch"


def limit(message):
    return ServiceError("RESOURCE_LIMIT", message)


class Budget:
    def __init__(self, size, message):
        self.remaining = size
        self.message = message

    def take(self, size):
        self.remaining -= size
        if self.remaining < 0:
            raise limit(self.message)
