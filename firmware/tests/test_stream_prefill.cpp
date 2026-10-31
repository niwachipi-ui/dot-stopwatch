#include <cassert>
#include <cstdio>
#include "../src/stream_playback_queue.h"

int main() {
    StreamPlaybackPrefill p;
    assert(!p.ready(0, 7, 1));
    assert(!p.ready(1499, 7, 1));
    assert(p.ready(1500, 7, 1)); // A short reply never waits indefinitely.
    p.reset();
    assert(!p.ready(2000, 7, 1));
    assert(p.ready(2200, 7, 2)); // A fast link need not wait the entire window.
    p.reset(); // OFF/cancel discards the previous wait.
    assert(!p.ready(5000, 7, 1));
    assert(!p.ready(6500, 8, 1)); // New epoch starts a fresh wait.
    assert(p.ready(8000, 8, 1));
    assert(!p.ready(8100, 8, 0));
    assert(!p.ready(9000, 8, 1)); // Empty queue also resets the wait.
    assert(!p.ready(11000, 0, 2)); // Invalid epoch cannot open output.
    p.reset();
    assert(!p.ready(UINT32_MAX - 500, 9, 1));
    assert(!p.ready(998, 9, 1));
    assert(p.ready(999, 9, 1)); // millis() wraparound.

    // Recorded acceptance times from the user's interrupted 15.944 s reply.
    // Model continuous PCM consumption, comparing immediate output to prefill.
    const unsigned arrival[] = {0,1590,3138,5133,6298,8209,9450,11587,12822,14048,15287};
    const unsigned duration[] = {1500,1500,1500,1500,1500,1500,1500,1500,1500,1500,944};
    unsigned immediateEnd = 0, bufferedEnd = StreamPlaybackPrefill::maxWaitMs;
    unsigned immediateGaps = 0, bufferedGaps = 0;
    for (unsigned i = 0; i < 11; ++i) {
        if (arrival[i] > immediateEnd) { immediateGaps += arrival[i] - immediateEnd; immediateEnd = arrival[i]; }
        if (arrival[i] > bufferedEnd) { bufferedGaps += arrival[i] - bufferedEnd; bufferedEnd = arrival[i]; }
        immediateEnd += duration[i]; bufferedEnd += duration[i];
    }
    assert(immediateGaps > 1000 && bufferedGaps == 0);
    std::printf("prefill: short reply, fast link, cancel, epoch, wrap; recorded timeline gaps %u -> %u ms\n", immediateGaps, bufferedGaps);
}
