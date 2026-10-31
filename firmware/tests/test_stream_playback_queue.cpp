#include <cassert>
#include <cstdio>
#include "../src/stream_playback_queue.h"

int main() {
    StreamPlaybackQueue q;
    assert(q.size() == 0 && q.freeSlot() == 0 && q.headSlot() == -1);
    assert(!q.commit(0, 1, 7, 0, 0));
    assert(!q.commit(0, 1, 7, 32001, 0));
    assert(!q.commit(1, 1, 7, 32000, 0));
    assert(q.commit(0, 1, 7, 32000, 200));
    assert(!q.commit(1, 2, 8, 32000, 200)); // Never mix epochs.
    assert(q.commit(1, 2, 7, 32000, 200));
    assert(q.commit(2, 3, 7, 1, 20));
    assert(q.freeSlot() == -1 && !q.commit(-1, 4, 7, 1, 20));
    assert(!q.markSubmitted(1));
    assert(q.markSubmitted(0) && q.markSubmitted(1));
    assert(!q.markSubmitted(2)); // Native speaker accepts at most two.
    assert(!q.retire(1)); // Complete in FIFO order.
    assert(q.retire(0));
    assert(q.at(q.headSlot()).id == 2 && q.freeSlot() == 0);
    assert(q.commit(0, 4, 7, 16000, 100)); // Reuse only the retired slot.
    assert(q.markSubmitted(2));
    assert(q.retire(1));
    assert(q.markSubmitted(0));
    assert(q.retire(2) && q.retire(0));
    assert(q.size() == 0 && q.submitted() == 0 && !q.retire(0));

    // Repeated wraparound, varying queue occupancy and completions.
    uint32_t accepted = 4, done = 4;
    for (unsigned cycle = 0; cycle < 100000; ++cycle) {
        while (q.freeSlot() >= 0) assert(q.commit(q.freeSlot(), ++accepted, 9, 16000, 100));
        while (q.pending() && q.submitted() < 2) assert(q.markSubmitted(q.pendingSlot()));
        assert(q.at(q.headSlot()).id == ++done);
        assert(q.retire(q.headSlot()));
    }
    // Cancellation metadata cannot make an active pointer reusable by itself;
    // only the owner's explicit speaker-stopped path clears the queue.
    assert(q.size() > 0);
    q.clearAfterSpeakerStopped();
    assert(q.size() == 0 && q.submitted() == 0 && q.freeSlot() == 0);
    assert(q.commit(0, ++accepted, 10, 32000, 300));
    assert(!q.markSubmitted(1));
    assert(q.markSubmitted(0));

    const int16_t pcm[] = {-32752, 32751, 1000, -1000};
    const auto metrics = CaptureMetrics::measure(pcm, 4);
    assert(metrics.peak == 32752 && metrics.clipped == 2 && metrics.dc == 0);
    assert(metrics.meanSquare == (uint64_t(32752)*32752 + uint64_t(32751)*32751 + 2000000)/4);
    const int16_t dc[] = {100, 100, 100};
    assert(CaptureMetrics::measure(dc, 3).dc == 100);
    assert(CaptureMetrics::measure(nullptr, 0).peak == 0);
    std::puts("stream queue capacity, FIFO, epoch, wraparound, cancel and retained PCM metrics passed");
}
