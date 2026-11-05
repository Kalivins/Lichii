# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded readers for shader containers observed in the owner's loose RomFS dumps.

BND4 reference: SoulsFormatsNEXT, BinderFileHeader.ReadBinder4FileHeader (MIT).
SHN/SHDROUNC and CGSB extents were checked against captured NVN2 files.
The SHARCB ("BAHS") entry layout is inferred, not documented; parse_sharcb lists the evidence.
"""

import ctypes
import ctypes.util
import struct
import zlib

from shader_archive import ArchiveError


MAX_ALLOCATION = 512 * 1024 * 1024
STAGES = ("vertex", "tess-control", "tess-eval", "geometry", "fragment")


def extent(data, offset, size):
    if offset < 0 or size < 0 or offset > len(data) or size > len(data) - offset:
        raise ArchiveError(f"extent {offset:#x}+{size:#x} outside {len(data):#x} bytes")
    return data[offset:offset + size]


def zstd_dictionary_id(data):
    """The dictionary ID a zstd frame header names, or 0 when it names none."""
    extent(data, 0, 6)
    if data[:4] != bytes.fromhex("28b52ffd"):
        raise ArchiveError("not a zstd frame")
    descriptor = data[4]
    size = (0, 1, 2, 4)[descriptor & 3]
    offset = 5 + (0 if descriptor & 0x20 else 1)
    return int.from_bytes(extent(data, offset, size), "little") if size else 0


def zstd_decompress(data, dictionaries=None):
    """Decompresses one zstd frame. `dictionaries` maps dictionary IDs to raw dictionary bytes
    (load_zstd_dictionaries); a frame whose header names a dictionary needs it."""
    dictionary_id = zstd_dictionary_id(data)
    dictionary = None
    if dictionary_id:
        dictionary = (dictionaries or {}).get(dictionary_id)
        if dictionary is None:
            raise ArchiveError(f"zstd frame needs dictionary {dictionary_id:#x}; pass the game's "
                               "dictionaries (Pack/ZsDic.pack.zs)")
    try:
        from compression import zstd
    except ImportError:
        library = ctypes.CDLL(ctypes.util.find_library("zstd"))
        library.ZSTD_getFrameContentSize.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        library.ZSTD_getFrameContentSize.restype = ctypes.c_ulonglong
        size = library.ZSTD_getFrameContentSize(data, len(data))
        if size > MAX_ALLOCATION:
            raise ArchiveError("unknown or excessive zstd decompressed size")
        output = ctypes.create_string_buffer(size)
        library.ZSTD_createDCtx.restype = ctypes.c_void_p
        library.ZSTD_freeDCtx.argtypes = [ctypes.c_void_p]
        library.ZSTD_decompress_usingDict.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
                                                      ctypes.c_void_p, ctypes.c_size_t,
                                                      ctypes.c_void_p, ctypes.c_size_t]
        library.ZSTD_decompress_usingDict.restype = ctypes.c_size_t
        context = library.ZSTD_createDCtx()
        try:
            actual = library.ZSTD_decompress_usingDict(context, output, size, data, len(data),
                                                       dictionary, len(dictionary or b""))
        finally:
            library.ZSTD_freeDCtx(context)
        if actual != size:
            raise ArchiveError("zstd decompression failed")
        return output.raw[:actual]
    size = zstd.get_frame_info(data).decompressed_size
    if size is None or size > MAX_ALLOCATION:
        raise ArchiveError("unknown or excessive zstd decompressed size")
    decoder = zstd.ZstdDecompressor(zstd.ZstdDict(dictionary) if dictionary else None)
    result = decoder.decompress(data, max_length=size + 1)
    if len(result) != size:
        raise ArchiveError("zstd decompressed size mismatch")
    return result


def load_zstd_dictionaries(data):
    """Reads a dictionary pack (Pack/ZsDic.pack.zs: a zstd frame holding a SARC of `.zsdic`
    files) or one raw dictionary, and returns {dictionary ID: dictionary bytes}."""
    from shader_archive import parse_sarc

    if data[:4] == bytes.fromhex("28b52ffd"):
        data = zstd_decompress(data)
    members = parse_sarc(data) if data[:4] == b"SARC" else [("dictionary", data)]
    result = {}
    for name, payload in members:
        # A zstd dictionary starts with its magic, then its ID.
        if payload[:4] != bytes.fromhex("37a430ec") or len(payload) < 8:
            if name.endswith(".zsdic") or name == "dictionary":
                raise ArchiveError(f"{name} is not a zstd dictionary")
            continue
        dictionary_id, = struct.unpack_from("<I", payload, 4)
        result[dictionary_id] = payload
    if not result:
        raise ArchiveError("no zstd dictionaries found")
    return result


def lz4_block(data, expected):
    if expected > MAX_ALLOCATION:
        raise ArchiveError("excessive LZ4 allocation")
    output = bytearray()
    cursor = 0

    def length(initial):
        nonlocal cursor
        value = initial
        if initial == 15:
            while True:
                if cursor >= len(data):
                    raise ArchiveError("truncated LZ4 length")
                extra = data[cursor]
                cursor += 1
                value += extra
                if extra != 255:
                    break
        return value

    while cursor < len(data):
        token = data[cursor]
        cursor += 1
        literals = length(token >> 4)
        if len(output) + literals > expected:
            raise ArchiveError("LZ4 literals exceed allocation")
        output.extend(extent(data, cursor, literals))
        cursor += literals
        if cursor == len(data):
            break
        distance, = struct.unpack("<H", extent(data, cursor, 2))
        cursor += 2
        count = length(token & 15) + 4
        if not distance or distance > len(output) or len(output) + count > expected:
            raise ArchiveError("invalid LZ4 match")
        while count:
            size = min(distance, count)
            output.extend(output[-distance:][:size])
            count -= size
    if len(output) != expected:
        raise ArchiveError("LZ4 decompressed size mismatch")
    return bytes(output)


def dcx_decompress(data):
    extent(data, 0, 0x30)
    if data[:4] != b"DCX\0" or data[0x18:0x1c] != b"DCS\0" or data[0x24:0x28] != b"DCP\0":
        raise ArchiveError("unsupported DCX header")
    offset, = struct.unpack_from(">I", data, 0x14)
    size, compressed = struct.unpack_from(">II", data, 0x1c)
    if size > MAX_ALLOCATION:
        raise ArchiveError("excessive DCX allocation")
    payload = extent(data, offset, compressed)
    if data[0x28:0x2c] == b"ZSTD":
        result = zstd_decompress(payload)
    elif data[0x28:0x2c] == b"DFLT":
        decoder = zlib.decompressobj()
        result = decoder.decompress(payload, size + 1)
        if not decoder.eof or decoder.unused_data:
            raise ArchiveError("incomplete or trailing DCX zlib stream")
    else:
        raise ArchiveError(f"unsupported DCX compressor {data[0x28:0x2c]!r}")
    if len(result) != size:
        raise ArchiveError("DCX decompressed size mismatch")
    return result


def parse_bnd4(data):
    extent(data, 0, 64)
    if data[:4] != b"BND4" or data[9:12] != b"\0\1\0" or data[48:52] != b"\1t\0\0":
        raise ArchiveError("unsupported BND4 byte order or file-record format")
    count, = struct.unpack_from("<I", data, 12)
    header, record_size = struct.unpack_from("<Q8xQ", data, 16)
    if header != 64 or record_size != 36 or count > 100000:
        raise ArchiveError("unsupported BND4 record size or count")
    extent(data, 64, count * record_size)
    result = []
    for index in range(count):
        record = 64 + index * record_size
        if data[record:record + 8] not in (b"\x40\0\0\0\xff\xff\xff\xff", b"\xc0\0\0\0\xff\xff\xff\xff"):
            raise ArchiveError("unsupported BND4 member flags")
        size, unpacked, offset, ident, name = struct.unpack_from("<QQIII", data, record + 8)
        if size > MAX_ALLOCATION or unpacked > MAX_ALLOCATION or name % 2:
            raise ArchiveError("invalid BND4 member size or name")
        end = name
        while extent(data, end, 2) != b"\0\0":
            end += 2
            if end - name > 65536:
                raise ArchiveError("excessive BND4 filename")
        filename = data[name:end].decode("utf-16-le")
        payload = extent(data, offset, size)
        if data[record] == 0xc0:
            payload = dcx_decompress(payload)
        if len(payload) != unpacked:
            raise ArchiveError("BND4 member size mismatch")
        result.append((filename, payload))
    return result


def graphics_record(code, control, **metadata):
    if len(code) <= 128 or (len(code) - 128) % 16:
        raise ArchiveError("unsupported native graphics allocation")
    stage = (struct.unpack_from("<I", code)[0] >> 10) & 15
    if not 1 <= stage <= 5:
        raise ArchiveError("native envelope is not a verified graphics header")
    if len(control) < 8 or control[:8] != bytes.fromhex("34127698c9000000"):
        raise ArchiveError("unsupported native shader control version")
    return {"stage": STAGES[stage - 1], "code": code, "control": control, **metadata}


def parse_cgsb(data):
    extent(data, 0, 24)
    if data[:8] != b"CGSB\x33\x22\x11\0":
        raise ArchiveError("unsupported CGSB version")
    count, shader_stage = struct.unpack_from("<HH", data, 8)
    table, = struct.unpack_from("<Q", data, 16)
    if not 1 <= count <= 16:
        raise ArchiveError("invalid CGSB record count")
    if shader_stage > 7:
        raise ArchiveError(f"unsupported CGSB stage {shader_stage}")
    extent(data, table, count * 80)
    result = []
    for index in range(count):
        record = table + index * 80
        if data[record:record + 8] != b"NVN \1\0\0\0":
            raise ArchiveError("unsupported CGSB shader platform")
        code_offset, code_size, control_offset, control_size = struct.unpack_from("<4Q", data, record + 16)
        if shader_stage >= 5:
            stage = ("compute", "task", "mesh")[shader_stage - 5]
            result.append({"stage": stage, "code": extent(data, code_offset, code_size),
                           "control": extent(data, control_offset, control_size),
                           "variation": index, "envelope": f"unverified-{stage}",
                           "code_offset": code_offset, "control_offset": control_offset})
        else:
            item = graphics_record(extent(data, code_offset, code_size),
                                   extent(data, control_offset, control_size),
                                   variation=index, code_offset=code_offset, control_offset=control_offset)
            if item["stage"] != STAGES[shader_stage]:
                raise ArchiveError("CGSB stage disagrees with native header")
            result.append(item)
    return result


def parse_shn(data):
    extent(data, 0, 68)
    if data[:4] != b"SHN!":
        raise ArchiveError("not a SHN shader container")
    blocks = []
    for descriptor in (4, 36):
        offset, compressed, size = struct.unpack_from("<III", data, descriptor)
        blocks.append(lz4_block(extent(data, offset, compressed), size))
    metadata, code = blocks
    if metadata[:8] != b"SHDROUNC" or len(metadata) < 64:
        raise ArchiveError("unsupported SHN metadata format")
    version, variations = struct.unpack_from("<II", metadata, 8)
    if version != 20241128 or not 1 <= variations <= 65536:
        raise ArchiveError("unsupported SHDROUNC version or variation count")
    extent(metadata, 16, variations * 80)
    result = []
    for variation in range(variations):
        table, = struct.unpack_from("<Q", metadata, 48 + variation * 80)
        extent(metadata, table, 9 * 80)
        for index in range(9):
            record = table + index * 80
            code_offset, control_offset = struct.unpack_from("<QQ", metadata, record)
            code_size, control_size = struct.unpack_from("<II", metadata, record + 64)
            if not code_size and not control_size:
                continue
            payload = extent(code, code_offset, code_size)
            control = extent(metadata, control_offset, control_size)
            origin = {"variation": variation, "slot": index, "code_offset": code_offset,
                      "control_offset": control_offset}
            if index == 2:
                result.append({"stage": "compute", "code": payload, "control": control,
                               "envelope": "unverified-compute", **origin})
            elif index in (0, 1):
                item = graphics_record(payload, control, **origin)
                if item["stage"] != ("vertex", "fragment")[index]:
                    raise ArchiveError("SHDROUNC stage disagrees with native header")
                result.append(item)
            else:
                result.append({"stage": f"unknown-shdrounc-slot-{index}", "code": payload,
                               "control": control, "envelope": "unverified-shdrounc-slot", **origin})
    if not result:
        raise ArchiveError("SHDROUNC contains no supported shader records")
    return result


# agl shader type -> (NVN2 control stage, record stage); type 2 (geometry) was never observed.
SHARCB_TYPES = {0: (0, "vertex"), 1: (1, "fragment"), 3: (5, "compute"), 4: (6, "task"), 5: (7, "mesh")}
NVN2_CONTROL = bytes.fromhex("34127698c9000000")


def sharcb_entry(metadata, code, entry):
    entry_size, agl_type, zero, body = struct.unpack_from("<4I", extent(metadata, entry, 0x20))
    if zero or entry_size < 0x20 or body != entry_size - 0x10 or agl_type not in SHARCB_TYPES:
        raise ArchiveError(f"SHARCB entry {entry:#x} has an unsupported header")
    code_offset, control_size, relative = struct.unpack_from("<QIi", metadata, entry + 0x10)
    control_offset = entry + 0x10 + relative
    control = extent(metadata, control_offset, control_size)
    if control_size < 0x58 or control[:8] != NVN2_CONTROL:
        raise ArchiveError(f"SHARCB entry {entry:#x} does not point at an NVN2 control block")
    program_size, allocation = struct.unpack_from("<I8xI", control, 0x38)
    if not 0 < program_size <= allocation:
        raise ArchiveError(f"SHARCB entry {entry:#x} has invalid code sizes")
    program = extent(code, code_offset, allocation)
    control_stage, stage = SHARCB_TYPES[agl_type]
    if struct.unpack_from("<I", control, 0x54)[0] >> 16 != control_stage:
        raise ArchiveError(f"SHARCB entry {entry:#x} type disagrees with its control stage")
    origin = {"entry_offset": entry, "code_offset": code_offset, "control_offset": control_offset}
    if stage not in ("vertex", "fragment"):
        return entry_size, agl_type, {"stage": stage, "code": program, "control": control,
                                      "envelope": f"unverified-{stage}", **origin}
    item = graphics_record(program, control, **origin)
    if item["stage"] != stage:
        raise ArchiveError(f"SHARCB entry {entry:#x} type disagrees with native header")
    return entry_size, agl_type, item


def sharcb_programs(metadata, table):
    """Yield (name, agl types, first entry, variations) from the program table."""
    table_size, count = struct.unpack_from("<II", extent(metadata, table, 8))
    offset = table + 8
    for _ in range(count):
        size, name_length, stages, first = struct.unpack_from("<4I", extent(metadata, offset, 16))
        name = extent(metadata, offset + 16, name_length)
        macros = offset + 16 + name_length
        if size < 16 + name_length + 8 or not name.endswith(b"\0") or stages >> 6 or not stages:
            raise ArchiveError(f"SHARCB program {offset:#x} has an unsupported header")
        list_size, macro_count = struct.unpack_from("<II", extent(metadata, macros, 8))
        variations, macro = 1, macros + 8
        for _ in range(macro_count):
            macro_size, _, values = struct.unpack_from("<3I", extent(metadata, macro, 12))
            variations *= values
            macro += macro_size
        if macro != macros + list_size or macro > offset + size:
            raise ArchiveError(f"SHARCB program {offset:#x} has an invalid macro list")
        types = [index for index in range(6) if stages >> index & 1]
        yield name[:-1].decode("ascii"), types, first, variations
        offset += size
    if offset != table + table_size:
        raise ArchiveError("SHARCB program table size disagrees with its programs")


def parse_sharcb(data):
    """Read every program entry of an agl SHARCB ("BAHS", version 0xb), named by its program.

    Header: string table at 0x150 sized by u32 0x14, code section size at 0x18, metadata size
    at 0x1c; the code section follows the metadata. After the strings come the entry table and
    the program table, each u32 size and u32 count. An entry is u32 size, u32 agl type, u32 0,
    u32 size - 0x10, then u64 code offset, u32 control size and an i32 control offset from that
    body, at +0x68 or at a control shared with an earlier entry. A program is u32 size, u32 name
    length, u32 agl type mask, u32 first entry, its name, then three macro lists; the first one
    multiplies into its variations, and its entries are variation-major, stage-minor.
    The allocation is control +0x44 bytes: code (+0x38), then constants, as in BNSH, where all
    13,265 OoT programs have exactly that length. In OoT's 13 SHARCBs the programs cover the
    entries exactly, the entries attribute every nonzero code byte without overlap and use every
    control block; all of it is enforced, so a misread is an error instead of a partial manifest.
    The order of macros inside a variation index is not established; records keep the index.
    """
    extent(data, 0, 0x150)
    if data[:4] != b"BAHS" or struct.unpack_from("<I", data, 4)[0] != 0xb:
        raise ArchiveError("unsupported SHARCB version")
    size, = struct.unpack_from("<I", data, 8)
    strings, code_size, metadata_size = struct.unpack_from("<III", data, 0x14)
    if size != len(data) or metadata_size < 0x150 + strings or metadata_size + code_size != size:
        raise ArchiveError("invalid SHARCB section sizes")
    metadata, code = data[:metadata_size], data[metadata_size:]
    table = 0x150 + strings
    table_size, count = struct.unpack_from("<II", extent(metadata, table, 8))
    entries, offset = [], table + 8
    for _ in range(count):
        entry_size, agl_type, record = sharcb_entry(metadata, code, offset)
        entries.append((agl_type, record))
        offset += entry_size
    if offset != table + table_size:
        raise ArchiveError("SHARCB entry table size disagrees with its entries")
    result = []
    for name, types, first, variations in sharcb_programs(metadata, offset):
        if first != len(result):
            raise ArchiveError(f"SHARCB program {name} does not start after the previous one")
        for index in range(len(types) * variations):
            if first + index >= count or entries[first + index][0] != types[index % len(types)]:
                raise ArchiveError(f"SHARCB program {name} disagrees with its entries")
            result.append({"program": name, "variation": index // len(types),
                           **entries[first + index][1]})
    if len(result) != count:
        raise ArchiveError("SHARCB programs do not cover every entry")
    used = {record["control_offset"] for record in result}
    found = metadata.find(NVN2_CONTROL)
    while found >= 0:
        if found not in used:
            raise ArchiveError(f"SHARCB control block {found:#x} has no entry")
        found = metadata.find(NVN2_CONTROL, found + 1)
    allocations = {}
    for record in result:
        allocations[record["code_offset"]] = max(allocations.get(record["code_offset"], 0),
                                                 len(record["code"]))
    end = 0
    for offset in sorted(allocations) + [len(code)]:
        if offset < end:
            raise ArchiveError(f"SHARCB allocations overlap at {offset:#x}")
        if any(code[end:offset]):
            raise ArchiveError(f"SHARCB code {end:#x}..{offset:#x} belongs to no entry")
        end = offset + allocations.get(offset, 0)
    if not result:
        raise ArchiveError("SHARCB contains no supported shader records")
    return result
