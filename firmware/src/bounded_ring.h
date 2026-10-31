#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>
// Caller serializes access. Popped and discarded bytes are wiped.
struct BoundedRing {
    uint8_t* data = nullptr;
    size_t capacity = 0, head = 0, used = 0;
    void bind(uint8_t* storage, size_t size) { data = storage; capacity = size; clear(); }
    void clear() { if (data) memset(data, 0, capacity); head = used = 0; }
    size_t free() const { return capacity - used; }
    bool push(const uint8_t* src, size_t count) {
        if (!data || count > free() || (count && !src)) return false;
        if (!count) return true;
        size_t tail = (head + used) % capacity;
        const size_t first = count < capacity - tail ? count : capacity - tail;
        memcpy(data + tail, src, first);
        if (count > first) memcpy(data, src + first, count - first);
        used += count; return true;
    }
    // A null destination discards without retaining stale/cancelled PCM.
    size_t pop(uint8_t* dst, size_t count) {
        if (count > used) count = used;
        if (!count) return 0;
        const size_t first = count < capacity - head ? count : capacity - head;
        if (dst) memcpy(dst, data + head, first);
        memset(data + head, 0, first);
        if (count > first) {
            if (dst) memcpy(dst + first, data, count - first);
            memset(data, 0, count - first);
        }
        head += count;
        if (head >= capacity) head -= capacity;
        used -= count; return count;
    }
};
