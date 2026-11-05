# SPDX-License-Identifier: GPL-3.0-or-later

import hashlib
from pathlib import Path
import struct
import tempfile
import unittest

from nup_container import Nup
from shader_archive import ArchiveError
from shader_containers import load_zstd_dictionaries, zstd_decompress, zstd_dictionary_id

try:
    from compression import zstd
except ImportError:
    zstd = None


def sarc(members):
    """A little-endian SARC with the given (name, payload) members, as parse_sarc reads it."""
    names = b""
    offsets = []
    for name, _ in members:
        offsets.append(len(names) // 4)
        names += name.encode() + b"\0"
        names += bytes(-len(names) % 4)
    nodes = b""
    data = b""
    for (name, payload), name_offset in zip(members, offsets):
        nodes += struct.pack("<IIII", 0, 0x01000000 | name_offset, len(data), len(data) + len(payload))
        data += payload + bytes(-len(payload) % 8)
    sfat = b"SFAT" + struct.pack("<HHI", 12, len(members), 0x65) + nodes
    sfnt = b"SFNT" + struct.pack("<HH", 8, 0) + names
    data_offset = 0x14 + len(sfat) + len(sfnt)
    size = data_offset + len(data)
    return b"SARC" + struct.pack("<HHIIHH", 0x14, 0xfeff, size, data_offset, 0x100, 0) + sfat + sfnt + data


def nup(files):
    """A NUP with one base package whose RomFS root holds `files` ({name: (data, zstd?)})."""
    names = b""

    def intern(text):
        nonlocal names
        offset = len(names)
        names += text.encode()
        return offset, len(text.encode())

    blob_offset = 0x1000
    blob = b""
    file_records = b""
    for name, (data, compressed) in files.items():
        stored = zstd.compress(data) if compressed else data
        offset = blob_offset + len(blob)
        blob += stored + bytes(-len(stored) % 16)
        file_records += struct.pack("<IIQQQ", *intern(name), len(data), offset, len(stored))
        file_records += hashlib.sha256(data).digest() + bytes([1 if compressed else 0, 0]) + bytes(14)
    root_name = intern("")
    dir_records = struct.pack("<IIIIQ", *root_name, 0, len(files), 0)
    children = struct.pack(f"<{len(files)}I", *range(1, 1 + len(files)))
    package = struct.pack("<QIIIIBBHI", 0x0400486024DEC000, 0, 0xFFFFFFFF, 0, 0x10000, 0, 0, 0, 0)
    package += bytes(0x40 - len(package))
    needed = 0x30 + len(package) + len(dir_records) + len(file_records)
    pool_offset = needed + len(children)
    header = struct.pack("<IIQQQQQ", 0x01434F54, 1, 1, len(files), pool_offset, len(names), 0)
    toc = header + package + dir_records + file_records + children + names
    size = blob_offset + len(blob)
    superblock = b"NUP\x01" + struct.pack("<HHQQ", 0x80, 1, 0x80, len(toc))
    superblock += hashlib.sha256(toc).digest() + struct.pack("<QQII", blob_offset, size, 1, 0)
    superblock += bytes(0x80 - len(superblock))
    image = superblock + toc
    return image + bytes(blob_offset - len(image)) + blob


@unittest.skipIf(zstd is None, "needs Python 3.14 compression.zstd")
class DictionaryTests(unittest.TestCase):
    def setUp(self):
        samples = [f"shader {i} uses bank {i % 7} and sampler {i % 5}".encode() * 4 for i in range(400)]
        self.dictionary = zstd.train_dict(samples, 4096)
        self.payload = samples[3] + samples[9]

    def test_plain_frames_still_decompress_without_dictionaries(self):
        frame = zstd.compress(b"plain" * 100)
        self.assertEqual(zstd_dictionary_id(frame), 0)
        self.assertEqual(zstd_decompress(frame), b"plain" * 100)

    def test_dictionary_frames_name_their_dictionary(self):
        frame = zstd.compress(self.payload, zstd_dict=self.dictionary)
        self.assertEqual(zstd_dictionary_id(frame), self.dictionary.dict_id)
        with self.assertRaisesRegex(ArchiveError, "needs dictionary"):
            zstd_decompress(frame)
        dictionaries = {self.dictionary.dict_id: self.dictionary.dict_content}
        self.assertEqual(zstd_decompress(frame, dictionaries), self.payload)

    def test_dictionary_pack_is_a_compressed_sarc_of_zsdic(self):
        pack = zstd.compress(sarc([("zs.zsdic", self.dictionary.dict_content), ("readme.txt", b"x")]))
        self.assertEqual(load_zstd_dictionaries(pack),
                         {self.dictionary.dict_id: self.dictionary.dict_content})
        with self.assertRaises(ArchiveError):
            load_zstd_dictionaries(zstd.compress(sarc([("bad.zsdic", b"not a dictionary")])))


@unittest.skipIf(zstd is None, "needs Python 3.14 compression.zstd")
class NupTests(unittest.TestCase):
    def write(self, image):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "game.nup"
        path.write_bytes(image)
        return path

    def test_lists_and_reads_raw_and_zstd_members(self):
        files = {"raw.bin": (b"raw bytes", False), "packed.bin": (b"packed" * 200, True)}
        container = Nup(self.write(nup(files)))
        self.addCleanup(container.stream.close)
        self.assertEqual(container.packages[0]["kind"], "base")
        listed = dict(container.walk(container.packages[0]["romfs"]))
        self.assertEqual(sorted(listed), ["packed.bin", "raw.bin"])
        for name, (data, _) in files.items():
            self.assertEqual(container.read(listed[name]), data)

    def test_table_of_contents_hash_and_member_hash_are_checked(self):
        image = bytearray(nup({"raw.bin": (b"raw bytes", False)}))
        image[0x80 + 0x30] ^= 1
        with self.assertRaisesRegex(ArchiveError, "SHA-256"):
            Nup(self.write(bytes(image)))
        image = bytearray(nup({"raw.bin": (b"raw bytes", False)}))
        image[0x1000] ^= 1
        container = Nup(self.write(bytes(image)))
        self.addCleanup(container.stream.close)
        record = next(iter(container.walk(container.packages[0]["romfs"])))[1]
        with self.assertRaisesRegex(ArchiveError, "SHA-256"):
            container.read(record)

    def test_rejects_other_files(self):
        with self.assertRaises(ArchiveError):
            Nup(self.write(b"NCA3" + bytes(0x200)))


if __name__ == "__main__":
    unittest.main()
