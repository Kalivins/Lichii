// SPDX-FileCopyrightText: Copyright 2026 Eden Emulator Project
// SPDX-License-Identifier: GPL-3.0-or-later

#pragma once

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <span>
#include <string>
#include <unordered_set>

#include <fmt/format.h>

#include "common/common_types.h"

// EDEN_NX2_MESH_TRACE=<file> (measurement, off by default): how task and mesh draws reach the 3D
// engine, and where their arguments come from. Nothing is written until <file>.now exists; at the
// next frame end the trace starts, runs for EDEN_NX2_MESH_TRACE_FRAMES presented frames (default
// 4), appends its lines to <file> and removes <file>.now. One line per event, fields separated by
// spaces, numbers in hex where they are addresses or words:
//   W <frame>                                        a window starts
//   M <frame> <seq> <method> <code> <dirty> <segments> <fetched> <refreshed>
//       a 3D macro call that drew a mesh, read memory, took its parameters from more than one
//       command list segment, or runs a macro that drew a mesh before. <segments> lists
//       address:words runs of where the parameters were read; <fetched> are the words as the
//       command list fetch read them, <refreshed> as the macro saw them after the accurate-mode
//       re-read (which flushes GPU writes first). <dirty> is whether a segment was GPU-written.
//   R <frame> <seq> <source> <words> <fifoed> <dirty> <guest> <read>
//       a macro memory read (MME DMA): <guest> the guest copy before any flush, <read> what the
//       macro received
//   D <frame> <seq> <first> <count> <instances> <indexed> <indirect> <executed> <programs>
//       a draw with mesh shading enabled, seq -1 outside a macro; <programs> are the six stage
//       slots' program addresses (0 when the slot is off)
//   I <frame> <seq> <address> <bytes> <max_draws> <stride> <count_address>
//       the indirect form of the draw just before (arguments read by the GPU)
//   C <frame> <dispatch> <program> <grid> <indirect> <written storage buffers address:bytes>
//   F <frame> <macro calls> <mesh draws> <dispatches>  a traced frame ends
// GPU thread only: the macros and the draws must run on the same thread (the split is off in
// accurate GPU mode, or EDEN_NX2_SPLIT=0).
namespace VideoCommon::Nx2MeshTrace {

inline const char* const g_path = std::getenv("EDEN_NX2_MESH_TRACE");
inline const bool g_enabled = g_path != nullptr;

struct State {
    std::FILE* file{};
    u64 frame{};
    u32 frames_left{};
    bool armed{};
    // The macro call in progress.
    bool in_macro{};
    u64 seq{};
    u32 method{};
    u32 code{};
    bool dirty{};
    u32 mesh_draws_in_call{};
    u32 reads_in_call{};
    u32 segment_count{};
    std::string segments;
    std::string fetched;
    std::span<const u32> parameters;
    // Per traced frame.
    u64 macro_calls{};
    u64 mesh_draws{};
    u64 dispatches{};
    // Macro code positions that drew a mesh at least once (kept across windows).
    std::unordered_set<u32> mesh_macros;
};

inline State g_state;

inline bool Active() {
    return g_enabled && g_state.armed;
}

inline void Write(const std::string& line) {
    if (g_state.file != nullptr) {
        std::fputs(line.c_str(), g_state.file);
    }
}

inline std::string Words(std::span<const u32> words, size_t limit = 32) {
    std::string out;
    const size_t count = std::min(words.size(), limit);
    for (size_t i = 0; i < count; ++i) {
        out += fmt::format("{}{:x}", i ? "," : "", words[i]);
    }
    if (words.size() > count) {
        out += fmt::format(",+{}", words.size() - count);
    }
    return out.empty() ? std::string{"-"} : out;
}

/// Frame end, from the rasterizer's TickFrame.
inline void TickFrame() {
    if (!g_enabled) {
        return;
    }
    auto& s = g_state;
    const std::string now_path = std::string{g_path} + ".now";
    if (s.armed) {
        Write(fmt::format("F {} {} {} {}\n", s.frame, s.macro_calls, s.mesh_draws, s.dispatches));
        s.macro_calls = 0;
        s.mesh_draws = 0;
        s.dispatches = 0;
        if (--s.frames_left == 0) {
            s.armed = false;
            std::fclose(s.file);
            s.file = nullptr;
            std::error_code ec;
            std::filesystem::remove(now_path, ec);
        }
    } else {
        std::error_code ec;
        if (std::filesystem::exists(now_path, ec)) {
            s.file = std::fopen(g_path, "ab");
            if (s.file != nullptr) {
                const char* const frames = std::getenv("EDEN_NX2_MESH_TRACE_FRAMES");
                const unsigned long value = frames != nullptr ? std::strtoul(frames, nullptr, 10) : 0;
                s.frames_left = value != 0 ? static_cast<u32>(std::min(value, 600UL)) : 4U;
                s.armed = true;
                s.macro_calls = 0;
                s.mesh_draws = 0;
                s.dispatches = 0;
                Write(fmt::format("W {}\n", s.frame + 1));
            }
        }
    }
    ++s.frame;
}

/// A 3D macro call is about to run. `maxwell3d` gives each parameter's source address.
template <typename Engine>
inline void BeginMacro(const Engine& maxwell3d, u32 method, u32 code,
                       std::span<const u32> parameters) {
    auto& s = g_state;
    s.in_macro = true;
    ++s.seq;
    ++s.macro_calls;
    s.method = method;
    s.code = code;
    s.dirty = maxwell3d.AnyParametersDirty();
    s.mesh_draws_in_call = 0;
    s.reads_in_call = 0;
    s.parameters = parameters;
    s.fetched = Words(parameters);
    s.segments.clear();
    s.segment_count = 0;
    u64 run_start = 0;
    u64 next = 0;
    u32 run_words = 0;
    for (size_t i = 0; i < parameters.size(); ++i) {
        const u64 address = maxwell3d.GetMacroAddress(i);
        if (run_words != 0 && address == next) {
            ++run_words;
        } else {
            if (run_words != 0) {
                s.segments += fmt::format("{}{:x}:{}", s.segment_count > 1 ? "+" : "", run_start,
                                          run_words);
            }
            ++s.segment_count;
            run_start = address;
            run_words = 1;
        }
        next = address + 4;
    }
    if (run_words != 0) {
        s.segments += fmt::format("{}{:x}:{}", s.segment_count > 1 ? "+" : "", run_start,
                                  run_words);
    }
    if (s.segments.empty()) {
        s.segments = "-";
    }
}

/// The macro call ended (after its deferred draws).
inline void EndMacro() {
    auto& s = g_state;
    if (!s.in_macro) {
        return;
    }
    s.in_macro = false;
    if (!s.armed) {
        return;
    }
    if (s.mesh_draws_in_call != 0) {
        s.mesh_macros.insert(s.code);
    }
    const bool interesting = s.mesh_draws_in_call != 0 || s.reads_in_call != 0 ||
                             s.segment_count > 1 || s.mesh_macros.contains(s.code);
    if (interesting) {
        Write(fmt::format("M {} {} {:x} {:x} {} {} {} {}\n", s.frame, s.seq, s.method, s.code,
                          s.dirty ? 1 : 0, s.segments, s.fetched, Words(s.parameters)));
    }
}

/// A macro memory read (MME DMA). `guest` is the guest copy before any flush.
inline void NoteRead(u64 source, u32 words, bool fifoed, bool dirty, std::span<const u32> guest,
                     std::span<const u32> read) {
    auto& s = g_state;
    ++s.reads_in_call;
    Write(fmt::format("R {} {} {:x} {} {} {} {} {}\n", s.frame, s.in_macro ? s.seq : 0, source,
                      words, fifoed ? 1 : 0, dirty ? 1 : 0, Words(guest, 16), Words(read, 16)));
}

/// A draw with mesh shading enabled reached the draw manager.
inline void NoteMeshDraw(u32 first, u32 count, u32 instances, bool indexed, bool indirect,
                         bool executed, const std::array<u64, 6>& programs) {
    auto& s = g_state;
    ++s.mesh_draws;
    if (s.in_macro) {
        ++s.mesh_draws_in_call;
    }
    Write(fmt::format("D {} {} {} {} {} {} {} {} {:x},{:x},{:x},{:x},{:x},{:x}\n", s.frame,
                      s.in_macro ? static_cast<s64>(s.seq) : s64{-1}, first, count, instances,
                      indexed ? 1 : 0, indirect ? 1 : 0, executed ? 1 : 0, programs[0],
                      programs[1], programs[2], programs[3], programs[4], programs[5]));
}

/// The GPU-read arguments of an indirect mesh draw.
inline void NoteIndirect(u64 address, u64 bytes, u64 max_draws, u64 stride, u64 count_address) {
    auto& s = g_state;
    Write(fmt::format("I {} {} {:x} {} {} {} {:x}\n", s.frame,
                      s.in_macro ? static_cast<s64>(s.seq) : s64{-1}, address, bytes, max_draws,
                      stride, count_address));
}

/// A compute dispatch, with the storage buffers it may write.
inline void NoteDispatch(u64 program, const std::array<u32, 3>& grid, u64 indirect,
                         const std::string& written) {
    auto& s = g_state;
    Write(fmt::format("C {} {} {:x} {},{},{} {:x} {}\n", s.frame, s.dispatches, program, grid[0],
                      grid[1], grid[2], indirect, written.empty() ? std::string{"-"} : written));
    ++s.dispatches;
}

} // namespace VideoCommon::Nx2MeshTrace
