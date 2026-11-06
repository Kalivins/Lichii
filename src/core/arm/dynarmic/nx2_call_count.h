// SPDX-License-Identifier: GPL-3.0-or-later

#pragma once

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

#include "common/common_types.h"
#include "common/logging.h"
#include "core/core.h"
#include "core/hardware_properties.h"
#include "core/hle/kernel/k_process.h"
#include "core/memory.h"
#include "dynarmic/interface/A64/config.h"

/// EDEN_NX2_CALL_COUNT counts the entries into chosen blocks, each under a slot:
///  - <module>:<begin>:<end>:<stride> counts the block that starts at begin + k * stride (offsets
///    from the module's load address) under slot k. Ocarina of Time's NVN2 trampolines are
///    sdk:0x651de0:0x654c90:16, but the game does not call through them.
///  - <module>:<begin>:<end>:ptr counts the block whose address the module's 8-byte slot k in
///    [begin, end) holds under slot k. The slots are read while the game runs; each time they
///    change, the lookup is replaced and every core's JIT cache is cleared, so that blocks
///    translated before get their counter. Ocarina of Time's NVN2 function pointers are
///    main:0x4d46f00:0x4d485f8:ptr, 735 slots filled by the game's NVN2 loader.
/// An entry counts however the block is entered: a call from inside the driver counts as much as
/// one from the game. Slots holding the same address count under the first of them.
/// Once the module is loaded, a thread appends a line to EDEN_NX2_CALL_COUNT_OUT (default
/// nx2_call_count.txt in the working directory) every EDEN_NX2_CALL_COUNT_PERIOD_MS (default
/// 1000): milliseconds since the module loaded, frames presented so far, then the total entries
/// of each slot. Calls per frame are the difference of two lines over their presents. In ptr mode
/// the slots' addresses are written to the same path with .slots appended, each time they change.
namespace Core::Nx2CallCount {

inline constexpr size_t MaxSlots = 4096;
inline constexpr u64 NoEntry = Dynarmic::A64::UserConfig::BlockEntryCounter::no_entry;

struct Spec {
    std::string module;
    u64 begin{};
    u64 end{};
    u64 stride{};
    bool pointers{};
    size_t slots{};
    bool valid{};
};

/// Block addresses with their slot, sorted by address, one entry per address.
using Table = std::vector<std::pair<u64, u64>>;

inline std::array<std::atomic<u64>, MaxSlots> g_counts{};
inline std::atomic<const Table*> g_table{};
inline Dynarmic::A64::UserConfig::BlockEntryCounter g_counter{};

inline void CountEntry(u64 index) {
    if (index < MaxSlots) {
        g_counts[index].fetch_add(1, std::memory_order_relaxed);
    }
}

inline u64 IndexOf(u64 pc) {
    const Table* const table = g_table.load(std::memory_order_acquire);
    if (table == nullptr) {
        return NoEntry;
    }
    const auto it = std::lower_bound(table->begin(), table->end(), std::pair{pc, u64{0}});
    return it != table->end() && it->first == pc ? it->second : NoEntry;
}

/// Replaces the lookup with these (address, slot) pairs and returns how many addresses it holds.
/// The previous table is never freed: a translation on another core may still be reading it.
inline size_t Publish(Table entries) {
    std::sort(entries.begin(), entries.end());
    entries.erase(std::unique(entries.begin(), entries.end(),
                              [](const auto& a, const auto& b) { return a.first == b.first; }),
                  entries.end());
    const size_t size = entries.size();
    g_table.store(new Table{std::move(entries)}, std::memory_order_release);
    return size;
}

inline Spec ParseSpec() {
    Spec spec;
    const char* const text = std::getenv("EDEN_NX2_CALL_COUNT");
    if (text == nullptr || *text == '\0') {
        return spec;
    }
    const std::string_view value{text};
    const size_t first = value.find(':');
    if (first == std::string_view::npos || first == 0) {
        LOG_ERROR(Core_ARM, "EDEN_NX2_CALL_COUNT={}: want <module>:<begin>:<end>:<stride|ptr>",
                  value);
        return spec;
    }
    spec.module = std::string{value.substr(0, first)};
    const std::string numbers{value.substr(first + 1)};
    char* cursor = nullptr;
    spec.begin = std::strtoull(numbers.c_str(), &cursor, 0);
    spec.end = *cursor == ':' ? std::strtoull(cursor + 1, &cursor, 0) : 0;
    if (*cursor == ':' && std::string_view{cursor + 1} == "ptr") {
        spec.pointers = true;
        spec.stride = sizeof(u64);
        cursor += 4;
    } else {
        spec.stride = *cursor == ':' ? std::strtoull(cursor + 1, &cursor, 0) : 0;
    }
    if (*cursor != '\0' || spec.stride == 0 || spec.end <= spec.begin ||
        (spec.end - spec.begin) / spec.stride > MaxSlots) {
        LOG_ERROR(Core_ARM,
                  "EDEN_NX2_CALL_COUNT={}: want <module>:<begin>:<end>:<stride|ptr>, end above "
                  "begin, at most {} slots",
                  value, MaxSlots);
        return spec;
    }
    spec.slots = static_cast<size_t>((spec.end - spec.begin) / spec.stride);
    spec.valid = true;
    return spec;
}

inline const Spec& GetSpec() {
    static const Spec spec = [] {
        Spec parsed = ParseSpec();
        if (parsed.valid) {
            g_counter.index_of = &IndexOf;
            g_counter.count = &CountEntry;
        }
        return parsed;
    }();
    return spec;
}

/// The counter the JIT config points at, or nullptr when EDEN_NX2_CALL_COUNT is unset.
inline const Dynarmic::A64::UserConfig::BlockEntryCounter* JitCounter() {
    return GetSpec().valid ? &g_counter : nullptr;
}

inline std::string OutPath() {
    const char* const out = std::getenv("EDEN_NX2_CALL_COUNT_OUT");
    return out != nullptr && *out != '\0' ? out : "nx2_call_count.txt";
}

inline void StartWriter(const Spec& spec) {
    const char* const period_text = std::getenv("EDEN_NX2_CALL_COUNT_PERIOD_MS");
    const std::string path = OutPath();
    const long period = period_text != nullptr ? std::strtol(period_text, nullptr, 10) : 1000;
    std::thread{[path, slots = spec.slots, period = period > 0 ? period : 1000] {
        std::FILE* const file = std::fopen(path.c_str(), "w");
        if (file == nullptr) {
            LOG_ERROR(Core_ARM, "EDEN_NX2_CALL_COUNT: cannot write {}", path);
            return;
        }
        std::fprintf(file, "# ms presents then the entries of each of %zu slots\n", slots);
        const auto start = std::chrono::steady_clock::now();
        std::string line;
        for (;;) {
            std::this_thread::sleep_for(std::chrono::milliseconds{period});
            const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                std::chrono::steady_clock::now() - start);
            line = std::to_string(elapsed.count());
            line += ' ';
            line += std::to_string(g_nx2_present_count.load(std::memory_order_relaxed));
            for (size_t slot = 0; slot < slots; ++slot) {
                line += ' ';
                line += std::to_string(g_counts[slot].load(std::memory_order_relaxed));
            }
            line += '\n';
            std::fputs(line.c_str(), file);
            std::fflush(file);
        }
    }}.detach();
    LOG_INFO(Core_ARM, "EDEN_NX2_CALL_COUNT: {} slots, every {} ms to {}", spec.slots, period,
             path);
}

/// ptr mode: reads the slots every 500 ms. Each time they change, publishes the addresses they
/// hold, clears every core's JIT cache and writes the addresses to <out>.slots. Stops once the
/// slots hold addresses and have stayed unchanged for 60 s, which leaves room for a loader that
/// fills them in two passes; until then it holds the process, so stopping the emulation sooner is
/// not supported.
inline void StartSlotReader(Kernel::KProcess& process, u64 first, size_t slots) {
    std::thread{[&process, first, slots, path = OutPath() + ".slots"] {
        std::vector<u64> seen(slots);
        std::vector<u64> now(slots);
        for (int quiet = 0; quiet < 120;) {
            std::this_thread::sleep_for(std::chrono::milliseconds{500});
            process.GetMemory().ReadBlockUnsafe(first, now.data(), slots * sizeof(u64));
            if (now == seen) {
                quiet += std::any_of(now.begin(), now.end(), [](u64 value) { return value != 0; });
                continue;
            }
            quiet = 0;
            seen = now;
            Table entries;
            for (size_t slot = 0; slot < slots; ++slot) {
                if (now[slot] != 0) {
                    entries.emplace_back(now[slot], slot);
                }
            }
            const size_t filled = entries.size();
            const size_t addresses = Publish(std::move(entries));
            for (size_t core = 0; core < Core::Hardware::NUM_CPU_CORES; ++core) {
                if (auto* const arm = process.GetArmInterface(core); arm != nullptr) {
                    arm->ClearInstructionCache();
                }
            }
            if (std::FILE* const file = std::fopen(path.c_str(), "w"); file != nullptr) {
                for (size_t slot = 0; slot < slots; ++slot) {
                    std::fprintf(file, "%zu 0x%llx\n", slot,
                                 static_cast<unsigned long long>(now[slot]));
                }
                std::fclose(file);
            }
            LOG_INFO(Core_ARM,
                     "EDEN_NX2_CALL_COUNT: {} of {} slots filled, {} distinct addresses, JIT "
                     "caches cleared",
                     filled, slots, addresses);
        }
        LOG_INFO(Core_ARM, "EDEN_NX2_CALL_COUNT: slots unchanged for 60 s, no longer read");
    }}.detach();
}

/// Points the counter at the module once the loader has placed it.
inline void OnModuleLoaded(Kernel::KProcess& process, std::string_view module, u64 base) {
    const Spec& spec = GetSpec();
    if (!spec.valid || module != spec.module) {
        return;
    }
    if (spec.pointers) {
        LOG_INFO(Core_ARM, "EDEN_NX2_CALL_COUNT: {} at {:#x}, reading {} slots at {:#x}", module,
                 base, spec.slots, base + spec.begin);
        StartSlotReader(process, base + spec.begin, spec.slots);
    } else {
        Table entries;
        for (size_t slot = 0; slot < spec.slots; ++slot) {
            entries.emplace_back(base + spec.begin + slot * spec.stride, slot);
        }
        Publish(std::move(entries));
        LOG_INFO(Core_ARM, "EDEN_NX2_CALL_COUNT: {} at {:#x}, blocks {:#x}..{:#x} every {} bytes",
                 module, base, base + spec.begin, base + spec.end, spec.stride);
    }
    static std::atomic_flag started;
    if (!started.test_and_set()) {
        StartWriter(spec);
    }
}

} // namespace Core::Nx2CallCount
