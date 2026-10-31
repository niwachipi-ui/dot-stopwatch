#pragma once
#include <atomic>
#include <stdint.h>

// Separates a requested lock from the audio worker's completed stop-and-wipe.
// The only runtime caller of confirmQuiescent is the audio worker. Startup may
// confirm before a worker exists, after Mic.end and before audio can be used.
struct GuardLatch {
    std::atomic<bool> locked{true};
    std::atomic<bool> worker{false};
    std::atomic<bool> fatal{true};
    std::atomic<uint32_t> requested{1}, applied{0};

    uint32_t requestLock() {
        locked = true;
        return requested.fetch_add(1) + 1;
    }
    bool pending() const { return requested.load() != applied.load(); }
    bool healthy() const { return worker.load() && !fatal.load(); }
    bool unlockIfReady() {
        if (!healthy() || pending()) return false;
        locked = false;
        return true;
    }
    bool confirmQuiescent(uint32_t epoch, bool microphoneStopped, bool recordingWiped) {
        if (!microphoneStopped || !recordingWiped || !locked.load()
            || requested.load() != epoch) return false;
        applied = epoch;
        return true;
    }
    bool confirmed(uint32_t epoch) const {
        return locked.load() && requested.load() == epoch && applied.load() == epoch;
    }
};

// Executes the real state helper without microphone, GPIO, or worker tasks.
// The omitted confirm between request and verification models a pre-begin or
// stalled worker; advancing time alone can never manufacture an acknowledgement.
inline bool guardLatchSelfTest() {
    GuardLatch gate;
    if (!gate.locked.load() || !gate.pending() || gate.unlockIfReady()) return false;
    if (!gate.confirmQuiescent(1, true, true) || gate.pending() || gate.unlockIfReady()) return false;
    gate.fatal = false;
    if (gate.unlockIfReady()) return false; // Worker creation has not succeeded.
    gate.worker = true;
    if (!gate.unlockIfReady() || gate.locked.load()) return false;
    const uint32_t request = gate.requestLock();
    if (!gate.locked.load() || !gate.pending() || gate.confirmed(request) || gate.unlockIfReady()) return false;
    // Worker may have passed its physical-start check, but has not stopped yet.
    if (gate.confirmQuiescent(request, false, true) || !gate.pending()) return false;
    if (gate.confirmQuiescent(request, true, false) || !gate.pending()) return false;
    if (gate.confirmQuiescent(request - 1, true, true) || !gate.pending()) return false;
    // A timeout leaves exactly this pending state and still rejects unlock.
    if (gate.confirmed(request) || gate.unlockIfReady()) return false;
    if (!gate.confirmQuiescent(request, true, true) || !gate.confirmed(request)) return false;
    const uint32_t newer = gate.requestLock();
    if (gate.confirmQuiescent(request, true, true) || !gate.pending()) return false;
    if (!gate.confirmQuiescent(newer, true, true) || gate.pending()) return false;
    gate.fatal = true;
    if (gate.unlockIfReady() || !gate.locked.load()) return false;
    gate.fatal = false; gate.worker = false;
    return !gate.unlockIfReady() && gate.locked.load();
}
