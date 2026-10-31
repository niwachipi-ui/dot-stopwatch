#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstring>
#include <deque>
#include <vector>
#include "../src/bounded_ring.h"
#include "../src/network_rx_limits.h"
#include "../src/audio_protocol.h"

static void check(const BoundedRing& ring, const std::deque<uint8_t>& expected) {
    assert(ring.used == expected.size());
    assert(ring.used + ring.free() == ring.capacity);
    for (size_t i = 0; i < ring.capacity; ++i)
        assert(ring.data[(ring.head + i) % ring.capacity] == (i < expected.size() ? expected[i] : 0));
}

// Exercise both copy segments and every empty/full/head position, including
// discarded reads and rejected writes retaining the exact previous contents.
static void exhaustiveSmallRing() {
    for (size_t capacity : {size_t(1), size_t(2), size_t(3), size_t(37)}) {
        std::vector<uint8_t> storage(capacity), input(capacity + 1), output(capacity + 1);
        for (size_t i = 0; i < input.size(); ++i) input[i] = uint8_t(i + 1);
        for (size_t head = 0; head < capacity; ++head) {
            for (size_t used = 0; used <= capacity; ++used) {
                for (size_t count = 0; count <= capacity + 1; ++count) {
                    for (unsigned operation = 0; operation < 3; ++operation) {
                        BoundedRing ring; ring.bind(storage.data(), capacity);
                        assert(ring.push(input.data(), head));
                        assert(ring.pop(nullptr, head) == head);
                        assert(ring.push(input.data(), used));
                        std::deque<uint8_t> expected(input.begin(), input.begin() + used);
                        if (!operation) {
                            const auto before = storage;
                            const bool fits = count <= ring.free();
                            assert(ring.push(input.data(), count) == fits);
                            if (fits) expected.insert(expected.end(), input.begin(), input.begin() + count);
                            else assert(storage == before);
                        } else {
                            const size_t take = std::min(count, used);
                            std::fill(output.begin(), output.end(), uint8_t(0xa5));
                            assert(ring.pop(operation == 1 ? output.data() : nullptr, count) == take);
                            for (size_t i = 0; i < take; ++i) {
                                if (operation == 1) assert(output[i] == expected.front());
                                expected.pop_front();
                            }
                            assert(output[take] == 0xa5);
                        }
                        check(ring, expected);
                        assert(!ring.push(nullptr, 1));
                        check(ring, expected);
                    }
                }
            }
        }
    }
    BoundedRing absent;
    assert(!absent.push(nullptr, 0));
    assert(absent.pop(nullptr, 1) == 0);
    uint8_t sentinel = 0xa5;
    absent.bind(&sentinel, 0);
    assert(absent.push(nullptr, 0) && !absent.push(&sentinel, 1));
    assert(absent.pop(&sentinel, 1) == 0 && sentinel == 0xa5);
}

static void fullBurst(bool cancel, bool corrupt) {
    using namespace NetworkRxLimits;
    std::vector<uint8_t> storage(rxBytes), payload(streamBytes), incoming(streamBytes, 0);
    std::vector<uint8_t> tail(commandBytes, uint8_t('x'));
    // Include command-like bytes inside PCM: only the exact known body length
    // may be consumed; no newline in PCM becomes a parser boundary.
    for (size_t i = 0; i < payload.size(); ++i) payload[i] = uint8_t(i * 31u + 17u);
    const char embedded[] = "\naudio_guard 0\ncall_ack 00000000 1 active 5000\n";
    memcpy(payload.data() + 4000, embedded, sizeof(embedded));
    const auto expectedCrc = AudioProtocol::crc32(payload.data(), payload.size());
    if (corrupt) payload[frameBytes - 1] ^= 1;
    tail.back() = '\n';
    BoundedRing ring; ring.bind(storage.data(), storage.size());
    // Begin near the end so both 4 KB input frames and output copies wrap.
    assert(ring.push(payload.data(), 63000));
    assert(ring.pop(nullptr, 63000) == 63000);
    for (size_t at = 0; at < payload.size(); at += frameBytes)
        assert(ring.push(payload.data() + at, std::min(frameBytes, payload.size() - at)));
    assert(ring.push(tail.data(), tail.size()));
    assert(ring.used == streamBytes + commandBytes);
    const auto before = storage;
    assert(!ring.push(payload.data(), ring.free() + 1));
    assert(storage == before); // Overload never makes a partial message visible.
    size_t received = 0, passes = 0;
    while (received < payload.size()) {
        size_t budget = pumpBytes;
        while (budget && received < payload.size()) {
            const size_t count = std::min(frameBytes, std::min(budget, payload.size() - received));
            const bool stale = cancel && received >= frameBytes;
            if (stale) std::fill(incoming.begin(), incoming.end(), uint8_t(0));
            const size_t read = ring.pop(stale ? nullptr : incoming.data() + received, count);
            assert(read == count);
            received += read; budget -= read;
        }
        ++passes;
    }
    assert(passes == 2 && ring.used == tail.size());
    if (cancel) assert(std::all_of(incoming.begin(), incoming.end(), [](uint8_t c) { return c == 0; }));
    else assert((AudioProtocol::crc32(incoming.data(), incoming.size()) == expectedCrc) == !corrupt);
    std::vector<uint8_t> actual(tail.size());
    assert(ring.pop(actual.data(), actual.size()) == actual.size() && actual == tail);
    assert(ring.used == 0);
    assert(std::all_of(storage.begin(), storage.end(), [](uint8_t c) { return c == 0; }));
    // Disconnect/reset wipes every byte, even if no body was processed.
    assert(ring.push(payload.data(), payload.size()));
    ring.clear();
    assert(ring.used == 0 && ring.head == 0);
    assert(std::all_of(storage.begin(), storage.end(), [](uint8_t c) { return c == 0; }));
}

int main() {
    using namespace NetworkRxLimits;
    static_assert(!supportsBurst(32768), "Old allocation cannot advertise burst");
    static_assert(!supportsBurst(streamBytes + commandBytes - 1), "Require framing headroom");
    static_assert(supportsBurst(rxBytes), "New allocation supports bounded burst");
    exhaustiveSmallRing();
    fullBurst(false, false);
    fullBurst(false, true);
    fullBurst(true, false);
    std::puts("PASS: exhaustive wrapped bulk ring, discard/zeroization/overflow, 64 KB burst+tail, CRC corruption, cancelled tail");
}
