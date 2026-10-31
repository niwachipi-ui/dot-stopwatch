#pragma once
#include <atomic>
#include <stdint.h>
#include "call_control.h"
#include "guard_latch.h"

// Capture the request before a bounded mutex wait, then validate it again once
// the mutex is held. Elapsed time alone must not extend a call's authority.
struct AudioCommandScope {
    uint32_t boot, request, epoch;
    const char* rejection(uint32_t currentBoot, uint32_t currentEpoch,
                          const CallControl& call, const GuardLatch& guard,
                          uint32_t now) const {
        if (boot != currentBoot || request != call.request || epoch != currentEpoch)
            return "audio_stale";
        if (!call.enabled.load() || !call.wanted || call.expired(now) || guard.locked.load())
            return "call_required";
        if (!guard.healthy()) return "guard_fault";
        if (guard.pending()) return "guard_pending";
        return nullptr;
    }
};

struct CapturePublication {
    static bool consume(std::atomic<bool>& requested, std::atomic<int>& state, int recording) {
        if (!requested.load()) return false;
        // A reader must always see either the outstanding request or Recording.
        state.store(recording);
        return requested.exchange(false);
    }
    static bool listening(const std::atomic<bool>& requested,
                          const std::atomic<int>& state, int recording) {
        // Match the writer's order: if request has been cleared, Recording was
        // already published. Reading state first would reopen the idle window.
        return requested.load() || state.load() == recording;
    }
};
