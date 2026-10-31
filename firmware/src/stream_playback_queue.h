#pragma once
#include <stddef.h>
#include <stdint.h>

// Audio-worker-owned startup cushion. Even a lone short final chunk starts
// within maxWaitMs. The caller still enforces call/guard/epoch output gates.
class StreamPlaybackPrefill {
public:
    static constexpr uint32_t maxWaitMs = 1500;
    bool ready(uint32_t now, uint32_t epoch, size_t chunks) {
        if (!chunks || !epoch) { reset(); return false; }
        if (!waiting_ || epoch_ != epoch) {
            waiting_ = true; epoch_ = epoch; since_ = now;
        }
        return chunks >= 2 || uint32_t(now - since_) >= maxWaitMs;
    }
    void reset() { waiting_ = false; epoch_ = since_ = 0; }
private:
    bool waiting_ = false;
    uint32_t epoch_ = 0, since_ = 0;
};

// Metadata only. The owner serializes access and keeps slot PCM alive until a
// submitted entry is retired, or the speaker task has stopped before clear().
class StreamPlaybackQueue {
public:
    static constexpr size_t slots = 3;
    static constexpr size_t maxSamples = 32000;
    struct Entry {
        uint32_t id = 0, epoch = 0;
        size_t samples = 0;
        int peak = 0;
    };
    size_t size() const { return count_; }
    size_t submitted() const { return submitted_; }
    size_t pending() const { return count_ - submitted_; }
    int freeSlot() const { return count_ < slots ? int((head_ + count_) % slots) : -1; }
    int pendingSlot() const { return submitted_ < count_ ? int((head_ + submitted_) % slots) : -1; }
    int headSlot() const { return count_ ? int(head_) : -1; }
    const Entry& at(size_t slot) const { return entries_[slot]; }
    bool commit(int slot, uint32_t id, uint32_t epoch, size_t samples, int peak) {
        if (slot != freeSlot() || slot < 0 || !id || !epoch || !samples ||
            samples > maxSamples || (count_ && entries_[head_].epoch != epoch)) return false;
        Entry& entry = entries_[slot];
        entry.id = id; entry.epoch = epoch; entry.samples = samples; entry.peak = peak;
        ++count_;
        return true;
    }
    bool markSubmitted(int slot) {
        if (slot != pendingSlot() || slot < 0 || submitted_ >= 2) return false;
        ++submitted_;
        return true;
    }
    bool retire(int slot) {
        if (slot != headSlot() || slot < 0 || !submitted_) return false;
        entries_[slot] = Entry{};
        head_ = (head_ + 1) % slots;
        --count_; --submitted_;
        return true;
    }
    void clearAfterSpeakerStopped() {
        for (auto& entry : entries_) entry = Entry{};
        head_ = count_ = submitted_ = 0;
    }
private:
    Entry entries_[slots];
    size_t head_ = 0, count_ = 0, submitted_ = 0;
};

struct CaptureMetrics {
    uint32_t peak = 0, clipped = 0, meanSquare = 0;
    int32_t dc = 0;
    static CaptureMetrics measure(const int16_t* data, size_t samples) {
        CaptureMetrics result;
        if (!data || !samples) return result;
        uint64_t squareSum = 0;
        int64_t sum = 0;
        for (size_t i = 0; i < samples; ++i) {
            const int32_t value = data[i];
            const uint32_t magnitude = value < 0 ? -value : value;
            if (magnitude > result.peak) result.peak = magnitude;
            if (value <= -32752 || value >= 32751) ++result.clipped;
            sum += value;
            squareSum += int64_t(value) * value;
        }
        result.dc = sum / int64_t(samples);
        result.meanSquare = squareSum / samples;
        return result;
    }
};
