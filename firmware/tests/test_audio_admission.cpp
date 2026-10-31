#include <cassert>
#include <cstdio>
#include <cstring>
#include <thread>
#include "../src/audio_admission.h"

int main() {
    GuardLatch guard;
    guard.worker=true; guard.fatal=false;
    assert(guard.confirmQuiescent(guard.requested.load(),true,true));
    assert(guard.unlockIfReady());
    CallControl call;
    call.on(1000); call.acknowledge(CallControl::Active,5000,1000);
    const AudioCommandScope scope{0x12345678,call.request,7};
    assert(!scope.rejection(0x12345678,7,call,guard,5999));
    // Authorization valid before a wait can expire during that wait.
    assert(!std::strcmp(scope.rejection(0x12345678,7,call,guard,6000),"call_required"));
    assert(!std::strcmp(scope.rejection(0x12345679,7,call,guard,1001),"audio_stale"));
    assert(!std::strcmp(scope.rejection(0x12345678,8,call,guard,1001),"audio_stale"));
    call.off(1002); call.on(1003); call.acknowledge(CallControl::Active,5000,1003);
    assert(!std::strcmp(scope.rejection(0x12345678,7,call,guard,1004),"audio_stale"));
    const AudioCommandScope newer{0x12345678,call.request,7};
    assert(!newer.rejection(0x12345678,7,call,guard,1004));
    guard.requestLock();
    assert(!std::strcmp(newer.rejection(0x12345678,7,call,guard,1004),"call_required"));
    guard.locked=false; // Model a pending stop whose lock was not yet observed.
    assert(!std::strcmp(newer.rejection(0x12345678,7,call,guard,1004),"guard_pending"));
    guard.fatal=true;
    assert(!std::strcmp(newer.rejection(0x12345678,7,call,guard,1004),"guard_fault"));

    std::atomic<bool> requested{true};
    std::atomic<int> state{0};
    assert(CapturePublication::listening(requested,state,1));
    assert(CapturePublication::consume(requested,state,1));
    assert(!requested && state==1 && CapturePublication::listening(requested,state,1));
    state=0;
    assert(!CapturePublication::consume(requested,state,1));
    assert(state==0 && !CapturePublication::listening(requested,state,1));

    // Concurrent reader across the real publication helper's handover: there
    // must never be an Idle observation between request and recording state.
    std::atomic<unsigned> phase{0}, readerReady{0}, readerDone{0};
    constexpr unsigned cycles=100000;
    std::thread reader([&] {
        for(unsigned cycle=1;cycle<=cycles;++cycle) {
            while(phase.load()!=cycle*2-1) std::this_thread::yield();
            readerReady=cycle;
            while(phase.load()==cycle*2-1)
                assert(CapturePublication::listening(requested,state,1));
            assert(CapturePublication::listening(requested,state,1));
            readerDone=cycle;
        }
    });
    for(unsigned cycle=1;cycle<=cycles;++cycle) {
        state=0; requested=true; phase=cycle*2-1;
        while(readerReady.load()!=cycle) std::this_thread::yield();
        assert(CapturePublication::consume(requested,state,1));
        phase=cycle*2;
        while(readerDone.load()!=cycle) std::this_thread::yield();
    }
    reader.join();
    std::puts("post-wait call/scope/guard checks and capture-state publication passed");
}
