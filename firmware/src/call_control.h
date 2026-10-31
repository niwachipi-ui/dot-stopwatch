#pragma once
#include <atomic>
#include <stdint.h>
#include <string.h>

// Main-loop-owned control state; only enabled is read by the audio worker.
// This describes the watch's audio relay, never the native app's phone call.
struct CallControl {
    enum State { Off, Wait, Ready, Active, Error };
    static constexpr uint32_t maxLease = 30000;
    uint32_t request = 0, updatedAt = 0, lease = 0;
    bool wanted = false;
    State state = Off;
    std::atomic<bool> enabled{false};

    const char* name() const {
        return state == Wait ? "wait" : state == Ready ? "ready"
            : state == Active ? "active" : state == Error ? "error" : "off";
    }
    uint32_t remaining(uint32_t now) const {
        const uint32_t age = now - updatedAt;
        return lease && age < lease ? lease - age : 0;
    }
    void on(uint32_t now) {
        enabled = false; ++request; wanted = true; state = Wait;
        updatedAt = now; lease = maxLease;
    }
    void off(uint32_t now) {
        enabled = false;
        // Every physical OFF invalidates acknowledgements already in flight.
        ++request; wanted = false; state = Off; updatedAt = now; lease = 0;
    }
    bool expired(uint32_t now) const { return wanted && !remaining(now); }
    bool accepts(uint32_t id, State next, uint32_t duration, uint32_t now) const {
        if (id != request || expired(now)) return false;
        if (next == Off) return !wanted && duration == 0;
        if (!wanted || duration < 1000 || duration > maxLease) return false;
        if (state == Error && next != Error) return false;
        // Errors require a new deliberate ON, not a late ACTIVE acknowledgement.
        return next != Active || state == Wait || state == Ready || state == Active;
    }
    void acknowledge(State next, uint32_t duration, uint32_t now) {
        state = next; updatedAt = now; lease = duration;
        enabled = next == Active;
    }
};

inline bool callControlSelfTest() {
    CallControl c;
    if (c.enabled || c.wanted || c.state != CallControl::Off) return false;
    c.on(100);
    const uint32_t first = c.request;
    if (!c.accepts(first, CallControl::Active, 30000, 101) || c.enabled) return false;
    if (c.accepts(first, CallControl::Active, 0, 101)
        || c.accepts(first, CallControl::Active, 30001, 101)) return false;
    c.off(102);
    if (c.enabled || c.wanted || c.accepts(first, CallControl::Active, 30000, 103)) return false;
    c.on(104);
    if (c.accepts(first, CallControl::Active, 30000, 105)) return false;
    c.acknowledge(CallControl::Active, 30000, 105);
    if (!c.enabled || c.expired(30104) || !c.expired(30105)) return false;
    if (c.accepts(c.request, CallControl::Active, 30000, 30105)) return false;
    c.off(30105);
    c.on(UINT32_MAX - 20);
    if (!c.remaining(4) || c.expired(4)) return false;
    c.acknowledge(CallControl::Error, 1000, 5);
    return !c.enabled && !c.accepts(c.request, CallControl::Active, 1000, 6);
}
