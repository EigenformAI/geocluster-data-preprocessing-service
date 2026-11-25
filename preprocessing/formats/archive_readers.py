"""Enumerate bounded ZIP and TAR members without extracting container paths."""
import gzip
import io
import re
import stat
import struct
import tarfile
import zipfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass

from preprocessing.archive_limits import ARCHIVE_ENTRIES, Budget, limit
from preprocessing.errors import ServiceError

CHUNK = 32 * 1024
ZIP_DIRECTORY_BYTES = 1024 * 1024
TAR_METADATA_BYTES = 16 * 1024
ZIP_LOCAL = struct.Struct("<4s5H3I2H")
ZIP_CENTRAL = struct.Struct("<4s6H3I5H2I")
ZIP_END = struct.Struct("<4s4H2IH")
ZIP64_LOCATOR = struct.Struct("<4sIQI")
ZIP64_END = struct.Struct("<4sQ2H2I4Q")
TAR_IGNORED_PAX = {b"mtime", b"atime", b"ctime", b"uid", b"gid", b"uname", b"gname", b"comment", b"charset"}
TAR_OTHER = {tarfile.LNKTYPE, tarfile.SYMTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE}


def malformed():
    return ServiceError("INVALID_INPUT", "Archive is malformed or does not match its filename suffix.")


def unsupported(message):
    return ServiceError("UNSUPPORTED_FORMAT", message)


@dataclass
class Member:
    name: str
    kind: str
    size: int
    blocks: Iterable[bytes]
    reason: str | None = None


def exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise malformed()
    return data


class ZipInput(io.BufferedReader):
    def read(self, size=-1):
        if size > ZIP_DIRECTORY_BYTES:
            raise limit("ZIP directory exceeds its byte limit.")
        if size < 0:
            data = super().read(ZIP_DIRECTORY_BYTES + 1)
            if len(data) > ZIP_DIRECTORY_BYTES:
                raise limit("ZIP directory exceeds its byte limit.")
            return data
        return super().read(size)


def zip_directory(stream):
    # Check the counts and byte range before ZipFile constructs one object per
    # directory entry. Require the end record to account for the full trailer.
    length = stream.seek(0, 2)
    start = max(0, length - ZIP_END.size - 65535)
    stream.seek(start)
    tail = stream.read()
    at = tail.rfind(b"PK\x05\x06")
    if at < 0 or len(tail) - at < ZIP_END.size:
        raise malformed()
    _, disk, central_disk, local_entries, entries, size, offset, comment = ZIP_END.unpack_from(tail, at)
    if at + ZIP_END.size + comment != len(tail):
        raise malformed()
    end = start + at
    stream.seek(max(0, end - ZIP64_LOCATOR.size))
    locator = stream.read(min(end, ZIP64_LOCATOR.size))
    if len(locator) == ZIP64_LOCATOR.size and locator.startswith(b"PK\x06\x07"):
        _, locator_disk, record_at, disks = ZIP64_LOCATOR.unpack(locator)
        if locator_disk or disks != 1:
            raise unsupported("Split ZIP archives are unsupported.")
        end -= ZIP64_LOCATOR.size + ZIP64_END.size
        if end != record_at:
            raise unsupported("ZIP64 uses unsupported end record extensions.")
        stream.seek(end)
        sig, record_size, _, _, disk, central_disk, local_entries, entries, size, offset = ZIP64_END.unpack(
            exact(stream, ZIP64_END.size))
        if sig != b"PK\x06\x06" or record_size != ZIP64_END.size - 12:
            raise malformed()
    if disk or central_disk or local_entries != entries:
        raise unsupported("Split ZIP archives are unsupported.")
    if entries > ARCHIVE_ENTRIES or size > ZIP_DIRECTORY_BYTES:
        raise limit("ZIP exceeds its directory byte or entry count limit.")
    if offset + size != end:
        raise malformed()
    stream.seek(offset)
    for _ in range(entries):
        record = ZIP_CENTRAL.unpack(exact(stream, ZIP_CENTRAL.size))
        if record[0] != b"PK\x01\x02" or record[13] != 0:
            raise malformed()
        if stream.seek(sum(record[10:13]), 1) > end:
            raise malformed()
    if stream.tell() != end:
        raise malformed()
    return entries, offset


def zip_kind(info):
    # Unix mode bits may be absent, even when the creator was Unix.
    mode = stat.S_IFMT(info.external_attr >> 16) if info.create_system in {3, 19} else 0
    directory = info.filename.endswith("/") or bool(info.external_attr & 0x10) or mode == stat.S_IFDIR
    if mode not in {0, stat.S_IFREG, stat.S_IFDIR} or info.external_attr & (0x08 | 0x400):
        return "other"
    if directory and (mode == stat.S_IFREG or info.file_size):
        raise malformed()
    return "directory" if directory else "file"


def zip_extra_fields(data):
    extra = memoryview(data)
    while extra:
        if len(extra) < 4:
            raise malformed()
        kind, length = struct.unpack_from("<HH", extra)
        end = 4 + length
        if end > len(extra):
            raise malformed()
        yield kind, extra[4:end]
        extra = extra[end:]


def zip_data_offset(stream, info, boundary, metadata):
    stream.seek(info.header_offset)
    sig, _, flags, method, _, _, crc, packed, size, name_bytes, extra_bytes = ZIP_LOCAL.unpack(
        exact(stream, ZIP_LOCAL.size))
    metadata.take(ZIP_LOCAL.size + name_bytes + extra_bytes)
    if sig != b"PK\x03\x04" or flags != info.flag_bits or method != info.compress_type:
        raise malformed()
    name = exact(stream, name_bytes).decode("utf-8" if flags & 0x800 else "cp437")
    if name != info.orig_filename:
        raise malformed()
    sizes = [size, packed]
    wide = 0xffffffff in sizes
    expanded = None
    for kind, data in zip_extra_fields(exact(stream, extra_bytes)):
        if kind == 1 and wide:
            if expanded is not None or len(data) != 8 * sizes.count(0xffffffff):
                raise malformed()
            expanded = iter(struct.unpack("<" + "Q" * (len(data) // 8), data))
    if wide:
        if expanded is None:
            raise malformed()
        size, packed = [next(expanded) if field == 0xffffffff else field for field in sizes]
    offset = stream.tell()
    end = offset + info.compress_size
    if end > boundary:
        raise malformed()
    if flags & 8:
        # Standard data descriptors may omit the signature. ZIP64 descriptors
        # are accepted when the local header opted into ZIP64 sizes.
        if (crc not in {0, info.CRC} or packed not in {0, info.compress_size}
                or size not in {0, info.file_size}):
            raise malformed()
        layout = struct.Struct("<IQQ" if wide else "<III")
        length = boundary - end
        if length not in {layout.size, layout.size + 4}:
            raise malformed()
        stream.seek(end)
        if length != layout.size and exact(stream, 4) != b"PK\x07\x08":
            raise malformed()
        if layout.unpack(exact(stream, layout.size)) != (info.CRC, info.compress_size, info.file_size):
            raise malformed()
    elif (end, crc, packed, size) != (boundary, info.CRC, info.compress_size, info.file_size):
        raise malformed()
    return offset


def zip_blocks(stream, info, start, budget):
    # ZipExtFile stops at the *declared* size and can hide actual inflation.
    # Stream just this validated local data range and check EOF, size and CRC.
    stream.seek(start)
    inflate = zlib.decompressobj(-zlib.MAX_WBITS) if info.compress_type == zipfile.ZIP_DEFLATED else None
    left, unpacked, crc = info.compress_size, 0, 0
    raw = b""
    try:
        while True:
            if not raw and left:
                raw = exact(stream, min(CHUNK, left))
                left -= len(raw)
            if not raw and (not inflate or inflate.eof):
                break
            block = inflate.decompress(raw, CHUNK) if inflate else raw
            tail = inflate.unconsumed_tail if inflate else b""
            budget.take(len(block))
            unpacked += len(block)
            if unpacked > info.file_size or (inflate and inflate.unused_data):
                raise malformed()
            crc = zlib.crc32(block, crc)
            if block:
                yield block
            elif tail == raw and not (inflate and inflate.eof):
                # No more compressed input or buffered output to advance on.
                break
            raw = tail
    except zlib.error:
        raise malformed() from None
    if left or (inflate and not inflate.eof) or unpacked != info.file_size or crc != info.CRC:
        raise malformed()


def zip_members(path, budget):
    try:
        with ZipInput(io.FileIO(path)) as stream:
            count, central_at = zip_directory(stream)
            with zipfile.ZipFile(stream) as archive:
                infos = archive.infolist()
                if count != len(infos) or central_at != archive.start_dir:
                    raise malformed()
            by_offset = sorted(info.header_offset for info in infos)
            if ((by_offset and by_offset[0] != 0) or (not by_offset and central_at != 0)
                    or len(set(by_offset)) != len(by_offset)):
                raise malformed()
            bounds = dict(zip(by_offset, [*by_offset[1:], central_at]))
            if sum(info.file_size for info in infos) > budget.remaining:
                raise limit(budget.message)
            metadata = Budget(ZIP_DIRECTORY_BYTES, "ZIP local metadata exceeds its byte limit.")
            for info in infos:
                if info.filename != info.orig_filename or info.header_offset >= central_at:
                    raise malformed()
                start = zip_data_offset(stream, info, bounds[info.header_offset], metadata)
                kind = zip_kind(info)
                reason = ("encrypted" if info.flag_bits & (1 | 64) else
                          "unsupported-compression" if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED} else
                          "unsupported-zip-feature" if info.flag_bits & (16 | 32 | 8192) else None)
                if reason:
                    budget.take(info.file_size)
                yield Member(info.filename, kind, info.file_size,
                             () if reason else zip_blocks(stream, info, start, budget), reason)
    except (zipfile.BadZipFile, UnicodeError, zlib.error, EOFError, ValueError):
        raise malformed() from None
    except NotImplementedError:
        raise unsupported("ZIP uses unsupported container features.") from None


class TarStream:
    def __init__(self, stream, budget):
        self.stream = stream
        self.budget = budget

    def read(self, size):
        try:
            data = self.stream.read(min(size, self.budget.remaining + 1))
        except (gzip.BadGzipFile, EOFError, zlib.error):
            raise malformed() from None
        self.budget.take(len(data))
        return data

    def blocks(self, size):
        if size > self.budget.remaining:
            raise limit(self.budget.message)
        while size:
            block = exact(self, min(size, CHUNK))
            size -= len(block)
            yield block

    def padding(self, size):
        if any(exact(self, -size % tarfile.BLOCKSIZE)):
            raise malformed()


def pax_name(data, *, global_header):
    name = None
    seen = set()
    while data:
        match = re.match(rb"([1-9][0-9]{0,5}) ", data)
        if not match:
            raise malformed()
        size = int(match[1])
        if not match.end() < size <= len(data) or data[size - 1:size] != b"\n":
            raise malformed()
        key, sep, value = data[match.end():size - 1].partition(b"=")
        if not sep or not key or key in seen:
            raise malformed()
        seen.add(key)
        if key == b"path" and not global_header:
            name = value.decode("utf-8")
        elif key not in TAR_IGNORED_PAX and not (key == b"linkpath" and not global_header):
            raise unsupported("TAR uses unsupported PAX metadata.")
        data = data[size:]
    return name


def tar_entries(stream):
    count = 0
    metadata = Budget(TAR_METADATA_BYTES, "TAR extended metadata exceeds its byte limit.")
    override = None
    pending = set()
    while True:
        header = exact(stream, tarfile.BLOCKSIZE)
        if not any(header):
            if pending or any(exact(stream, tarfile.BLOCKSIZE)):
                raise malformed()
            while trailing := stream.read(CHUNK):
                if any(trailing):
                    raise malformed()
            return
        count += 1
        if count > ARCHIVE_ENTRIES:
            raise limit("TAR exceeds its entry count limit (including extended headers).")
        # Header decoding is stdlib; member framing and all consumed reads are
        # ours, so the library cannot skip or allocate from untrusted lengths.
        header_type = header[156:157]
        if header_type == tarfile.GNUTYPE_SPARSE:
            raise unsupported("Sparse TAR members are unsupported.")
        info = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
        if info.size < 0:
            raise malformed()
        if info.type in {tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK}:
            if info.type in pending or (pending and info.type == tarfile.XGLTYPE):
                raise malformed()
            metadata.take(info.size)
            data = b"".join(stream.blocks(info.size))
            stream.padding(info.size)
            name = None
            if info.type in {tarfile.XHDTYPE, tarfile.XGLTYPE}:
                name = pax_name(data, global_header=info.type == tarfile.XGLTYPE)
            else:
                if not data.endswith(b"\0") or b"\0" in data[:-1]:
                    raise malformed()
                if info.type == tarfile.GNUTYPE_LONGNAME:
                    name = data[:-1].decode("utf-8")
            if name is not None:
                if override is not None:
                    raise malformed()
                override = name
            if info.type != tarfile.XGLTYPE:
                pending.add(info.type)
            continue
        if info.type in {tarfile.REGTYPE, tarfile.AREGTYPE}:
            kind = "file"
        elif info.type == tarfile.DIRTYPE:
            if info.size:
                raise malformed()
            kind = "directory"
        elif info.type in TAR_OTHER:
            kind = "other"
        else:
            raise unsupported("TAR uses unsupported member metadata or types.")
        # TarInfo strips directory suffixes and rebuilds names without
        # distinguishing the ustar prefix from old GNU timestamp fields.
        name = override
        if name is None:
            name = header[:100].split(b"\0", 1)[0].decode("utf-8")
            if header[257:263] == b"ustar\0":
                prefix = header[345:500].split(b"\0", 1)[0].decode("utf-8")
                if prefix:
                    name = f"{prefix}/{name}"
        override = None
        pending.clear()
        yield Member(name, kind, info.size, stream.blocks(info.size))
        stream.padding(info.size)


def tar_members(path, budget, *, compressed):
    opener = gzip.open if compressed else open
    try:
        with opener(path, "rb") as stream:
            yield from tar_entries(TarStream(stream, budget))
    except (tarfile.HeaderError, gzip.BadGzipFile, EOFError, zlib.error, UnicodeError, ValueError):
        raise malformed() from None
