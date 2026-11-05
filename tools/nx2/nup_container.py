#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read NUP (NX Package) containers read-only: list packages and copy out RomFS/ExeFS members.

The layout and its checks follow src/core/file_sys/nup_container.cpp: a 0x80-byte superblock,
a SHA-256-checked table of contents (packages, directories, files, children, string pool) and
16-byte aligned file extents stored raw or as one zstd frame. NUP members are plain files, so
nothing here needs keys. Games often compress their own files again (`.zs`), sometimes with
dictionaries; see shader_containers.zstd_decompress.

tools/shader-sweep/nup_export.cpp exports with the emulator's own reader but has to be built
first (build_nup_windows.py); this one needs only Python.
"""

import argparse
import hashlib
from pathlib import Path
import re
import struct
import sys

from shader_archive import ArchiveError
from shader_containers import MAX_ALLOCATION, zstd_decompress


SUPERBLOCK_SIZE = 0x80
TOC_HEADER_SIZE = 0x30
PACKAGE_SIZE = 0x40
DIR_SIZE = 0x18
FILE_SIZE = 0x50
TOC_MAGIC = 0x01434F54
NO_DIR = 0xFFFFFFFF
CODEC_RAW, CODEC_ZSTD = 0, 1
KINDS = ("base", "update", "addon")


class Nup:
    def __init__(self, path):
        self.path = Path(path)
        self.stream = open(self.path, "rb")
        try:
            self._open()
        except BaseException:
            self.stream.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.stream.close()

    def _open(self):
        size = self.path.stat().st_size
        superblock = self.stream.read(SUPERBLOCK_SIZE)
        if len(superblock) != SUPERBLOCK_SIZE or superblock[:4] != b"NUP\x01":
            raise ArchiveError("not a NUP container")
        header_size, = struct.unpack_from("<H", superblock, 4)
        toc_offset, toc_size = struct.unpack_from("<QQ", superblock, 8)
        toc_sha256 = superblock[0x18:0x38]
        self.blob_offset, file_size = struct.unpack_from("<QQ", superblock, 0x38)
        package_count, flags = struct.unpack_from("<II", superblock, 0x48)
        if (header_size < SUPERBLOCK_SIZE or flags or file_size != size or
                toc_offset < SUPERBLOCK_SIZE or toc_offset > self.blob_offset or
                self.blob_offset > size or toc_size > self.blob_offset - toc_offset or
                self.blob_offset % 0x10):
            raise ArchiveError("invalid NUP superblock")
        self.size = size
        self.stream.seek(toc_offset)
        toc = self.stream.read(toc_size)
        if len(toc) != toc_size or hashlib.sha256(toc).digest() != toc_sha256:
            raise ArchiveError("NUP table of contents is truncated or its SHA-256 differs")
        self._parse_toc(toc, package_count)

    def _parse_toc(self, toc, package_count):
        if len(toc) < TOC_HEADER_SIZE:
            raise ArchiveError("truncated NUP table of contents")
        magic, count, dir_count, file_count, pool_offset, pool_size = struct.unpack_from(
            "<IIQQQQ", toc, 0)
        if magic != TOC_MAGIC or count == 0 or count != package_count:
            raise ArchiveError("invalid NUP table of contents header")
        if dir_count > len(toc) // DIR_SIZE or file_count > len(toc) // FILE_SIZE:
            raise ArchiveError("NUP record counts exceed the table of contents")
        needed = TOC_HEADER_SIZE + count * PACKAGE_SIZE + dir_count * DIR_SIZE + file_count * FILE_SIZE
        if (needed > len(toc) or pool_offset < needed or pool_offset > len(toc) or
                pool_size > len(toc) - pool_offset or (pool_offset - needed) % 4):
            raise ArchiveError("invalid NUP table of contents extents")
        pool = toc[pool_offset:pool_offset + pool_size]

        def name(offset, length):
            if offset + length > pool_size:
                raise ArchiveError("NUP name outside the string pool")
            return pool[offset:offset + length].decode("utf-8")

        position = TOC_HEADER_SIZE
        self.packages = []
        for _ in range(count):
            title_id, root, exefs, romfs, version, kind, flags = struct.unpack_from(
                "<QIIIIBB", toc, position)
            position += PACKAGE_SIZE
            if (kind >= len(KINDS) or flags & ~1 or root >= dir_count or
                    (exefs != NO_DIR and exefs >= dir_count) or
                    (romfs != NO_DIR and romfs >= dir_count)):
                raise ArchiveError("invalid NUP package record")
            self.packages.append({"title_id": title_id, "version": version, "kind": KINDS[kind],
                                  "exefs": exefs, "romfs": romfs})
        children_count = (pool_offset - needed) // 4
        self.dirs = []
        for _ in range(dir_count):
            name_offset, name_length, first, child_count = struct.unpack_from("<IIII", toc, position)
            position += DIR_SIZE
            if first + child_count > children_count:
                raise ArchiveError("NUP directory children outside the table")
            self.dirs.append((name(name_offset, name_length), first, child_count))
        self.files = []
        for _ in range(file_count):
            name_offset, name_length, size, offset, stored = struct.unpack_from("<IIQQQ", toc, position)
            sha256 = toc[position + 0x20:position + 0x40]
            codec, codec_flags = toc[position + 0x40], toc[position + 0x41]
            position += FILE_SIZE
            if (codec > CODEC_ZSTD or codec_flags or
                    (size == 0 and (offset or stored)) or
                    (size and (not stored or offset < self.blob_offset or offset % 0x10 or
                               offset > self.size or stored > self.size - offset))):
                raise ArchiveError("invalid NUP file record")
            self.files.append({"name": name(name_offset, name_length), "size": size,
                               "offset": offset, "stored": stored, "codec": codec,
                               "sha256": sha256})
        self.children = struct.unpack_from(f"<{children_count}I", toc, needed)
        if any(child >= dir_count + file_count for child in self.children):
            raise ArchiveError("NUP child index outside the table")

    def walk(self, directory, prefix="", depth=0):
        """Yields (path, file record) under a directory index, depth first."""
        if depth > 128:
            raise ArchiveError("excessive NUP directory depth")
        _, first, count = self.dirs[directory]
        for slot in range(first, first + count):
            child = self.children[slot]
            if child >= len(self.dirs):
                record = self.files[child - len(self.dirs)]
                yield prefix + record["name"], record
            else:
                yield from self.walk(child, prefix + self.dirs[child][0] + "/", depth + 1)

    def read(self, record, verify=True):
        """Returns a member's bytes, undoing the NUP codec (not the game's own compression)."""
        if record["size"] > MAX_ALLOCATION or record["stored"] > MAX_ALLOCATION:
            raise ArchiveError("NUP member exceeds the in-memory limit")
        if not record["size"]:
            return b""
        self.stream.seek(record["offset"])
        data = self.stream.read(record["stored"])
        if record["codec"] == CODEC_ZSTD:
            data = zstd_decompress(data)
        if len(data) != record["size"]:
            raise ArchiveError("NUP member size differs from its record")
        if verify and hashlib.sha256(data).digest() != record["sha256"]:
            raise ArchiveError("NUP member SHA-256 differs from its record")
        return data


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("nup", type=Path)
    parser.add_argument("--tree", choices=("romfs", "exefs"), default="romfs")
    parser.add_argument("--package", choices=KINDS, help="only this package kind")
    parser.add_argument("--match", help="regular expression on member paths (case-insensitive)")
    parser.add_argument("--extract", type=Path,
                        help="new directory; members go to <dir>/<title>-<kind>-v<version>/<path>")
    args = parser.parse_args()
    if args.extract and args.extract.exists():
        parser.error("--extract must be a new directory")
    pattern = re.compile(args.match, re.IGNORECASE) if args.match else None
    nup = Nup(args.nup)
    for package in nup.packages:
        label = f"{package['title_id']:016X}-{package['kind']}-v{package['version']}"
        root = package[args.tree]
        print(f"{label}: {args.tree} {'absent' if root == NO_DIR else 'present'}")
        if root == NO_DIR or (args.package and package["kind"] != args.package):
            continue
        for path, record in nup.walk(root):
            if pattern and not pattern.search(path):
                continue
            print(f"  {record['size']:>12} {path}")
            if args.extract:
                target = (args.extract / label / path).resolve()
                if args.extract.resolve() not in target.parents:
                    raise ArchiveError("member path escapes the extraction directory")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(nup.read(record))
    return 0


if __name__ == "__main__":
    sys.exit(main())
