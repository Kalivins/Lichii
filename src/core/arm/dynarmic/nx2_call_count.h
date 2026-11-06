// SPDX-License-Identifier: GPL-3.0-or-later

#pragma once

#include <array>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <string_view>
#include <thread>

#include "common/common_types.h"
#include "common/logging.h"
#include "core/core.h"
#include "dynarmic/interface/A64/config.h"

/// EDEN_NX2_CALL_COUNT=<module>:<begin>:<end>:<stride> counts the entries into every block that
/// starts at begin + k * stride, offsets from the module's load address. Ocarina of Time's NVN2
/// trampolines are sdk:0x651de0:0x654c90:16 (one 16-byte trampoline per NVN2 function).
/// Once the module is loaded, a thread appends a line to EDEN_NX2_CALL_COUNT_OUT (default
/// nx2_call_count.txt in the working directory) every EDEN_NX2_CALL_COUNT_PERIOD_MS (default
/// 1000): milliseconds since the module loaded, frames presented so far, then the total entries
/// of each slot. Calls per frame are the difference of two lines over their presents.
namespace Core::Nx2CallCount {

inline constexpr size_t MaxSlots = 4096;

struct Spec {
    std::string module;
    u64 begin{};
    u64 end{};
    u64 stride{};
    size_t slots{};
    bool valid{};
};

inline std::array<std::atomic<u64>, MaxSlots> g_counts{};
inline Dynarmic::A64::UserConfig::BlockEntryCounter g_counter{};

inline void CountEntry(u64 index) {
    if (index < MaxSlots) {
        g_counts[index].fetch_add(1, std::memory_order_relaxed);
    }
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
        LOG_ERROR(Core_ARM, "EDEN_NX2_CALL_COUNT={}: want <module>:<begin>:<end>:<stride>", value);
        return spec;
    }
    spec.module = std::string{value.substr(0, first)};
    const std::string numbers{value.substr(first + 1)};
    char* cursor = nullptr;
    spec.begin = std::strtoull(numbers.c_str(), &cursor, 0);
    spec.end = *cursor == ':' ? std::strtoull(cursor + 1, &cursor, 0) : 0;
    spec.stride = *cursor == ':' ? std::strtoull(cursor + 1, &cursor, 0) : 0;
    if (*cursor != '\0' || spec.stride == 0 || spec.end <= spec.begin ||
        (spec.end - spec.begin) / spec.stride > MaxSlots) {
        LOG_ERROR(Core_ARM,
                  "EDEN_NX2_CALL_COUNT={}: want <module>:<begin>:<end>:<stride>, end above "
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
            g_counter.stride = parsed.stride;
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

inline void StartWriter(const Spec& spec) {
    const char* const out = std::getenv("EDEN_NX2_CALL_COUNT_OUT");
    const char* const period_text = std::getenv("EDEN_NX2_CALL_COUNT_PERIOD_MS");
    const std::string path = out != nullptr && *out != '\0' ? out : "nx2_call_count.txt";
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

/// Points the counter at the module once the loader has placed it.
inline void OnModuleLoaded(std::string_view module, u64 base) {
    const Spec& spec = GetSpec();
    if (!spec.valid || module != spec.module) {
        return;
    }
    g_counter.end.store(base + spec.end, std::memory_order_relaxed);
    g_counter.begin.store(base + spec.begin, std::memory_order_relaxed);
    LOG_INFO(Core_ARM, "EDEN_NX2_CALL_COUNT: {} at {:#x}, blocks {:#x}..{:#x} every {} bytes",
             module, base, base + spec.begin, base + spec.end, spec.stride);
    static std::atomic_flag started;
    if (!started.test_and_set()) {
        StartWriter(spec);
    }
}

} // namespace Core::Nx2CallCount
