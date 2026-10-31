#pragma once
#include <stddef.h>

namespace NetworkRxLimits {
constexpr size_t frameBytes = 4096;
constexpr size_t streamBytes = 64000;
constexpr size_t commandBytes = 384;
constexpr size_t rxBytes = 65536;
constexpr size_t txBytes = 32768;
constexpr size_t pumpBytes = 32768;
static_assert(rxBytes >= streamBytes + commandBytes, "RX must hold a full authorized stream chunk");
constexpr bool supportsBurst(size_t allocatedBytes) {
    return allocatedBytes >= streamBytes + commandBytes;
}
}
