"""Contained regular-file access and durable local publication primitives."""
import hashlib
import json
import os
import stat
from pathlib import Path, PurePosixPath

from preprocessing.errors import ServiceError


def relative_parts(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ServiceError('INVALID_PATH', 'A relative regular-file path is required.')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise ServiceError('INVALID_PATH', 'A contained relative path is required.')
    return path.parts


def open_regular(root, relative):
    parts = relative_parts(relative)
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ServiceError('SOURCE_READ_FAILED', 'Source must be a regular file.')
        return os.fdopen(fd, 'rb')
    finally:
        os.close(directory)


def digest_file(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)
