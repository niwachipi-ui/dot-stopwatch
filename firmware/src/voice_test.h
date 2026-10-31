#pragma once
#include <atomic>
#include <WiFi.h>
#include <soc/gpio_reg.h>
#include "audio_protocol.h"
#include "guard_latch.h"
#include "call_control.h"
#include "protocol_port.h"
#include "handsfree_controls.h"
#include "touch_controls.h"
#include "stream_playback_queue.h"
#include "audio_admission.h"
#include "network_rx_limits.h"
#include "dot_activity.h"
#include <math.h>

// v25: only a physical ON, matching active call lease, and epoch-bound listen
// request together permit hands-free capture. Boot/OFF/guard always disable it.
namespace VoiceTest {
// GPIO1 is electrically unstable on this device. Disable all blue-button
// actions; legacy protocol button fields describe the effective input. Raw
// pin level remains available as blue_raw, with diagnostic counters paused.
constexpr bool blueButtonEnabled = false;
bool bluePressed() { return blueButtonEnabled && digitalRead(1) == LOW; }
ProtocolPort* currentPort = &usbProtocol;
ProtocolPort* receivingPort = nullptr;
void (*transportService)() = nullptr;
void (*phoneOffHook)() = nullptr;
bool (*networkOwner)() = nullptr;
ProtocolPort& io() { return *currentPort; }
struct PortScope {
    ProtocolPort* previous;
    explicit PortScope(ProtocolPort& port) : previous(currentPort) { currentPort = &port; }
    ~PortScope() { currentPort = previous; }
};
constexpr size_t captureCapacity = VoiceAudioLimits::captureSamples;
constexpr size_t captureMaxBytes = captureCapacity * sizeof(int16_t);
constexpr size_t incomingCapacity = VoiceAudioLimits::incomingSamples;
constexpr size_t incomingMaxBytes = incomingCapacity * sizeof(int16_t);
constexpr uint32_t retentionMs = 30000;
enum State { Idle, Recording, Ready, Playing, Error, Receiving, Queued, Sending };
int16_t* audio = nullptr;
int16_t* incoming = nullptr;
SemaphoreHandle_t audioMutex = nullptr;
SemaphoreHandle_t playbackMutex = nullptr;
StreamPlaybackQueue playbackQueue;
int16_t* playbackSlots[StreamPlaybackQueue::slots] = {};
constexpr size_t streamMaxBytes = StreamPlaybackQueue::maxSamples * sizeof(int16_t);
static_assert(streamMaxBytes == NetworkRxLimits::streamBytes, "Burst capability must match playback limit");
static_assert(streamMaxBytes <= incomingMaxBytes, "Incoming allocation must hold each stream chunk");
std::atomic<size_t> streamCount{0};
std::atomic<bool> streamCancel{false}, streamSpeakerOpen{false};
std::atomic<uint32_t> playAccepted{0}, playDone{0};
std::atomic<uint32_t> streamFailedEpoch{0};
// Only the audio worker changes these hardware progress timestamps.
uint32_t streamProgressAt = 0, streamDrainingAt = 0;
StreamPlaybackPrefill streamPrefill;
std::atomic<int> state{Idle}, peak{0};
std::atomic<size_t> samples{0};
std::atomic<bool> replay{false}, consumed{false};
// Fail closed after every boot. Unlocking only permits a future physical press.
GuardLatch guardLatch;
CallControl call;
bool callTestPassed = false;
std::atomic<bool>& micGuard = guardLatch.locked;
std::atomic<uint32_t> armEpoch{0}, inhibitUntil{0};
std::atomic<uint32_t> blueDown{0}, blueUp{0}, blueQualified{0}, blueIgnored{0};
std::atomic<uint32_t> blueHighSamples{0}, blueLowSamples{0}, blueLongestLowMs{0}, blueLowSince{0}, blueMismatch{0};
std::atomic<uint32_t> recordings{0}, recordCrc{0}, completedAt{0};
std::atomic<uint32_t> lastCompletedSamples{0}, lastCompletedPeak{0};
std::atomic<uint32_t> lastCompletedClips{0}, lastCompletedRms{0}, lastEndReason{0};
std::atomic<int32_t> lastCompletedDc{0};
std::atomic<uint32_t> playId{0}, playResult{0}, playSamples{0}, playPeak{0}, playCount{0}, localPlays{0};
std::atomic<size_t> queuedSamples{0};
std::atomic<uint32_t> audioEpoch{1}, recordEpoch{0}, queuedEpoch{0};
std::atomic<bool> listenRequested{false}, interruptRequested{false}, wipeTurn{false};
std::atomic<int> volume{100};
int batteryPercent=-1, batteryMv=-1, batteryCharging=-1;
uint32_t batteryAt=0;
std::atomic<uint32_t> volumeAt{0};
bool batteryRead=false;
uint32_t receiveEpoch=0;
bool receiveStale=false;
uint32_t bootNonce = 0;
bool protocolTestPassed = false;
bool guardTestPassed = false;
// Main-loop-only receive state. The main task owns audioMutex until RX ends.
std::atomic<bool> receiving{false};
bool receiveStream = false;
uint32_t receiveExpected = 0, receiveCrc = 0, receiveAt = 0, receiveLastAt = 0;
size_t received = 0;
void abortReceive(const char* reason);

void cancelTurn() {
    listenRequested = false;
    ++audioEpoch; ++armEpoch;
    streamCancel = true;
    queuedSamples = 0; replay = false; wipeTurn = true;
    if (receiving) { receiveStale = true; memset(incoming, 0, incomingMaxBytes); }
}
void stopPhoneAudio() {
    call.enabled = false;
    cancelTurn();
    guardLatch.requestLock();
    ++armEpoch;
    replay = false;
    // RX is owned by this task. The worker owns recording/playback cleanup.
    if (receiving) abortReceive("call_stopped");
}
void phoneOff() {
    call.off(millis());
    stopPhoneAudio();
    if (phoneOffHook) phoneOffHook();
}
void phoneOn() {
    if (call.wanted && call.state != CallControl::Error) return;
    stopPhoneAudio();
    call.on(millis());
}
void adjustVolume(int delta) {
    int previous = volume.load(), next;
    do { next = std::max(0, std::min(255, previous+delta)); }
    while (!volume.compare_exchange_weak(previous,next));
    if (next != previous) { volumeAt=millis(); M5.Speaker.setVolume(volume.load()); }
}
// Release inside the same button; dragging over a control never activates it.
// Also called during bounded USB TX so OFF is not hidden behind a transfer.
bool updatePhoneControls() {
    if (transportService) transportService();
    if (interruptRequested.exchange(false) && call.enabled.load() && !micGuard.load()) cancelTurn();
    if (!batteryRead || millis()-batteryAt >= 2000) {
        batteryRead=true; batteryAt=millis();
        batteryPercent=M5.Power.getBatteryLevel(); batteryMv=M5.Power.getBatteryVoltage();
        batteryCharging=int(M5.Power.isCharging());
    }
    if (call.expired(millis())) phoneOff();
    static WatchTouchControls controls;
    const auto count = M5.Touch.getCount();
    if (!count) { controls.reset(); return false; }
    const auto touch = M5.Touch.getDetail();
    const auto action = controls.update(count,touch.isPressed(),touch.x,touch.y);
    if (action.volumeDelta) adjustVolume(action.volumeDelta);
    if (action.callButton == 1) phoneOn();
    else if (action.callButton == 2) phoneOff();
    return action.consumed;
}

// Called only by the voice task, including its arming/playback loops.
bool sampleBlue() {
    if (!blueButtonEnabled) return false;
    static bool wasPressed = false;
    const bool pressed = digitalRead(1) == LOW;
    if (pressed != (((REG_READ(GPIO_IN_REG) >> 1) & 1u) == 0)) ++blueMismatch;
    if (pressed) {
        ++blueLowSamples;
        if (!wasPressed) blueLowSince = millis();
        blueLongestLowMs = std::max(blueLongestLowMs.load(), uint32_t(millis() - blueLowSince.load()));
    } else { ++blueHighSamples; blueLowSince = 0; }
    if (pressed != wasPressed) {
        if (pressed) { ++blueDown; if (micGuard.load()) ++blueIgnored; }
        else ++blueUp;
        wasPressed = pressed;
    }
    return pressed;
}
void pollButtons() {
    static HoldButton blue, yellow;
    const auto up=blue.update(sampleBlue(), millis());
    const auto down=yellow.update(digitalRead(2)==LOW, millis());
    if (up==HoldButton::Short) {
        ++blueQualified;
        if (call.enabled.load() && !micGuard.load()) interruptRequested=true;
    }
    if(up==HoldButton::Volume) adjustVolume(10);
    if(down==HoldButton::Volume) adjustVolume(-10);
}
void speakerCooldown() { inhibitUntil = millis()+250; ++armEpoch; }

bool lock() { return audioMutex && xSemaphoreTake(audioMutex, 0) == pdTRUE; }
// Only command admission may wait briefly for idle housekeeping. Capture,
// playback and ordinary control polling retain their nonblocking lock paths.
bool lockAudioCommand() {
    return audioMutex && xSemaphoreTake(audioMutex, pdMS_TO_TICKS(30)) == pdTRUE;
}
void unlock() { xSemaphoreGive(audioMutex); }
bool lockPlayback() { return playbackMutex && xSemaphoreTake(playbackMutex, 0) == pdTRUE; }
void unlockPlayback() { xSemaphoreGive(playbackMutex); }
bool retained() { return samples.load() && millis() - completedAt.load() < retentionMs; }
void eraseRecording() { // Caller owns audioMutex; the microphone is stopped.
    if (audio) memset(audio, 0, captureMaxBytes);
    samples = 0; peak = 0; recordCrc = 0; completedAt = 0;
    // IDs, consumed status and non-content level metrics survive erasure.
}
void expireRecording() {
    if (samples.load() && !retained()) eraseRecording();
}
int restingState() { return retained() ? Ready : Idle; }
int outputState() {
    if (streamSpeakerOpen.load()) return Playing;
    if (streamCount.load() || queuedSamples.load()) return Queued;
    if (receiving.load()) return Receiving;
    return restingState();
}

bool streamAllowed(uint32_t epoch) {
    return call.enabled.load() && !micGuard.load() && !interruptRequested.load()
        && !streamCancel.load() && epoch == audioEpoch.load()
        && epoch != streamFailedEpoch.load();
}

// Audio worker only. stop() is asynchronous in M5Unified, so end() joins its
// task before any submitted PCM storage is wiped or made reusable.
void stopStreamPlayback(bool failed = false) {
    // A concurrent RX must not silently restart output after a hardware fault.
    // Only a later, explicit listen/new epoch can authorize playback again.
    if (failed) streamFailedEpoch = audioEpoch.load();
    const bool hadOutput = streamSpeakerOpen.load() || streamCount.load();
    if (streamSpeakerOpen.load()) {
        M5.Speaker.stop();
        M5.Speaker.end();
        streamSpeakerOpen = false;
        speakerCooldown();
    }
    if (playbackMutex && xSemaphoreTake(playbackMutex, portMAX_DELAY) == pdTRUE) {
        for (auto* slot : playbackSlots) if (slot) memset(slot, 0, streamMaxBytes);
        playbackQueue.clearAfterSpeakerStopped();
        streamCount = 0;
        unlockPlayback();
    }
    streamProgressAt = streamDrainingAt = 0;
    streamPrefill.reset();
    if (hadOutput) playResult = failed ? 5 : 4;
    // Main may be receiving the now-stale tail. It exclusively owns incoming.
    state = outputState();
}

// Audio worker only; never holds audioMutex across playback. All native output
// uses one channel so M5Unified's two-buffer queue joins adjacent chunks.
void serviceStreamPlayback() {
    if (streamCancel.exchange(false)) { stopStreamPlayback(); return; }
    if (!streamCount.load()) { streamPrefill.reset(); return; }
    if (!lockPlayback()) return;
    const auto head = playbackQueue.at(playbackQueue.headSlot());
    const size_t buffered = playbackQueue.size();
    unlockPlayback();
    if (!streamAllowed(head.epoch)) { stopStreamPlayback(); return; }
    if (!streamSpeakerOpen.load()) {
        if (M5.Mic.isRunning()) { stopStreamPlayback(true); return; }
        // A bounded cushion prevents network round-trip jitter from draining
        // the speaker queue immediately after the first packet starts.
        if (!streamPrefill.ready(millis(), head.epoch, buffered)) {
            state = Queued;
            return;
        }
        M5.Speaker.setVolume(volume.load());
        const bool started = M5.Speaker.begin();
        streamSpeakerOpen = started;
        if (!started) { M5.Speaker.end(); stopStreamPlayback(true); return; }
        streamProgressAt = millis();
        if (!streamAllowed(head.epoch)) { stopStreamPlayback(); return; }
    }
    if (!lockPlayback()) return;
    const size_t nativeCount = M5.Speaker.isPlaying(uint8_t(0));
    bool draining = false;
    if (playbackQueue.submitted() > nativeCount && nativeCount == 0 && !playbackQueue.pending()) {
        // isPlaying() becomes zero when PCM is copied to I2S DMA, before the
        // last samples reach the speaker. Keep the last slot live for 100 ms.
        if (!streamDrainingAt) streamDrainingAt = millis();
        draining = millis() - streamDrainingAt < 100;
    } else streamDrainingAt = 0;
    if (!draining) {
        while (playbackQueue.submitted() > nativeCount) {
            const int slot = playbackQueue.headSlot();
            const auto finished = playbackQueue.at(slot);
            memset(playbackSlots[slot], 0, streamMaxBytes);
            playbackQueue.retire(slot);
            playDone = finished.id; ++playCount; playResult = 3;
            streamProgressAt = millis();
        }
        // Publish empty only after the speaker task has joined below; this
        // keeps a host observing play_queue=0 from racing a final listen.
        if (playbackQueue.size()) streamCount = playbackQueue.size();
    }
    const int next = playbackQueue.submitted() < 2 ? playbackQueue.pendingSlot() : -1;
    StreamPlaybackQueue::Entry queued;
    if (next >= 0) queued = playbackQueue.at(next);
    const int current = playbackQueue.headSlot();
    if (current >= 0) {
        const auto& active = playbackQueue.at(current);
        playId = active.id; playSamples = active.samples; playPeak = active.peak;
        playResult = playbackQueue.submitted() ? 2 : 1;
    }
    const bool empty = playbackQueue.size() == 0;
    unlockPlayback();
    if (empty) {
        M5.Speaker.end();
        streamSpeakerOpen = false;
        speakerCooldown();
        xSemaphoreTake(playbackMutex, portMAX_DELAY);
        streamCount = playbackQueue.size(); // Main may have appended during end().
        unlockPlayback();
        streamProgressAt = streamDrainingAt = 0;
        streamPrefill.reset();
        state = outputState();
        return;
    }
    if (!streamAllowed(head.epoch)) { stopStreamPlayback(); return; }
    if (next >= 0) {
        const bool played = M5.Speaker.playRaw(playbackSlots[next], queued.samples, 16000, false, 1, 0, false);
        if (!played) { stopStreamPlayback(true); return; }
        // Only this task submits/retires. Main can append, but cannot replace
        // this entry or wipe its storage while the speaker is reading it.
        xSemaphoreTake(playbackMutex, portMAX_DELAY);
        const bool marked = playbackQueue.markSubmitted(next);
        unlockPlayback();
        if (!marked) { stopStreamPlayback(true); return; }
        streamProgressAt = millis(); streamDrainingAt = 0;
        playResult = 2;
    }
    if (millis() - streamProgressAt > 4500) { stopStreamPlayback(true); return; }
    state = Playing;
}

// Audio-worker-only runtime acknowledgement. Holding audioMutex makes the
// wipe exclusive, and this worker cannot concurrently enter its Mic.begin path.
// The speaker has a separate buffer; the phone gate cancels its playback loop.
void applyPendingGuard() {
    if (!micGuard.load() || (!guardLatch.pending() && !samples.load())) return;
    if (streamCount.load() || streamSpeakerOpen.load()) return;
    const uint32_t request = guardLatch.requested.load();
    M5.Mic.end();
    eraseRecording();
    guardLatch.confirmQuiescent(request, !M5.Mic.isRunning(), samples.load() == 0);
}

// ES8311 needs SYSTEM arming after I2S starts, per M5Unified upstream.
// https://github.com/m5stack/M5Unified/blob/master/src/M5Unified.inl
bool armCodec(uint32_t captureEpoch) {
    bool ok = M5.In_I2C.writeRegister8(0x18, 0x14, 0x17, 100000);
    ok = M5.In_I2C.writeRegister8(0x18, 0x17, 0xCB, 100000) && ok;
    ok = M5.In_I2C.writeRegister8(0x18, 0x1C, 0x64, 100000) && ok;
    for (int i = 0; i < 3; ++i) {
        for (int n = 0; n < 6; ++n) {
            pollButtons();
            if (micGuard.load() || !call.enabled.load() || interruptRequested.load() || armEpoch.load() != captureEpoch) return false;
            vTaskDelay(pdMS_TO_TICKS(5));
        }
        ok = M5.In_I2C.writeRegister8(0x18, 0x0D, 0x01, 100000) && ok;
    }
    return ok;
}

void playBuffer(const int16_t* data, size_t count, bool external) {
    if (!call.enabled.load()) { if (external) playResult = 4; state = restingState(); return; }
    state = Playing;
    if (external) playResult = 2;
    M5.Mic.end();
    const uint32_t turn=queuedEpoch.load();
    if (turn != audioEpoch.load()) { playResult=4; state=restingState(); return; }
    M5.Speaker.setVolume(volume.load());
    const bool started = M5.Speaker.begin() && M5.Speaker.playRaw(data, count, 16000);
    const uint32_t beganAt = millis();
    const uint32_t deadline = uint32_t(count * 1000 / 16000) + 2500;
    bool cancelled = false, timedOut = false;
    if (started) {
        while (M5.Speaker.isPlaying()) {
            if (external) applyPendingGuard();
            if (external) expireRecording();
            pollButtons();
            if (!call.enabled.load() || micGuard.load() || interruptRequested.load() || turn!=audioEpoch.load()) { cancelled = true; break; }
            if (millis() - beganAt >= deadline) { timedOut = true; break; }
            // A retained recording expires even during a long local replay.
            if (!external && !retained()) { cancelled = true; break; }
            vTaskDelay(pdMS_TO_TICKS(5));
        }
    }
    M5.Speaker.stop();
    M5.Speaker.end(); // Wait for speaker task before wiping/reusing its buffer.
    speakerCooldown();
    if (external) {
        playResult = !started || timedOut ? 5 : cancelled ? 4 : 3;
        if (started && !cancelled && !timedOut) { ++playCount; playDone = playId.load(); }
    } else if (started && !cancelled && !timedOut) ++localPlays;
    if (micGuard.load()) applyPendingGuard();
    else expireRecording();
    state = restingState();
}

void task(void*) {
    guardLatch.worker = true;
    uint32_t errorAt=0;
    int16_t blocks[3][EnergyVad::blockSamples] = {};
    for (;;) {
        pollButtons();
        serviceStreamPlayback();
        if (listenRequested.load() && !interruptRequested.load() && audio && call.enabled.load()
            && !micGuard.load() && !streamCount.load() && !streamSpeakerOpen.load()
            && static_cast<int32_t>(millis()-inhibitUntil.load())>=0 && lock()) {
            if (listenRequested.load() && !queuedSamples.load() && !streamCount.load()
                && !receiving.load() && !micGuard.load() && call.enabled.load()) {
                const uint32_t captureEpoch=armEpoch.load(), turn=audioEpoch.load();
                if (!CapturePublication::consume(listenRequested,state,Recording)) {
                    state=outputState();
                } else {
                    M5.Speaker.end(); eraseRecording(); consumed=false; wipeTurn=false;
                    const auto captureAllowed=[captureEpoch,turn]() {
                        return !micGuard.load() && call.enabled.load() && !interruptRequested.load()
                            && armEpoch.load()==captureEpoch && audioEpoch.load()==turn;
                    };
                    bool failed=!captureAllowed() || !M5.Mic.begin();
                    // This installed M5Unified sets _rec_sample_rate only when
                    // begin() is called while running. Settle it before arming so
                    // the first record() cannot reset the codec back to defaults.
                    if(!failed) failed=!captureAllowed() || !M5.Mic.begin();
                    if(!failed) failed=!armCodec(captureEpoch);
                    EnergyVad vad; size_t queued=0, stored=0, warmup=10;
                    uint32_t endReason=0;
                    bool complete=false;
                    while(!failed && !complete && call.enabled.load() && !micGuard.load()
                        && !interruptRequested.load() && armEpoch.load()==captureEpoch && audioEpoch.load()==turn) {
                        pollButtons();
                        const size_t slot=queued%3;
                        if(!M5.Mic.record(blocks[slot],EnergyVad::blockSamples,16000)) { failed=true; break; }
                        if(queued>=2) {
                            const auto* done=blocks[(queued-2)%3];
                            if (warmup) {
                                // Drain completed DMA blocks after codec changes;
                                // startup transients never enter VAD or pre-roll.
                                --warmup; ++queued; vTaskDelay(pdMS_TO_TICKS(1)); continue;
                            }
                            const bool ended=vad.update(done);
                            if(!vad.speech && stored>=EnergyVad::preRoll) {
                                memmove(audio,audio+EnergyVad::blockSamples,(stored-EnergyVad::blockSamples)*sizeof(int16_t));
                                stored-=EnergyVad::blockSamples;
                            }
                            if(stored+EnergyVad::blockSamples<=captureCapacity) {
                                memcpy(audio+stored,done,EnergyVad::blockSamples*sizeof(int16_t));
                                stored+=EnergyVad::blockSamples;
                            }
                            int level=0;
                            for(size_t i=0;i<EnergyVad::blockSamples;++i) level=std::max(level,abs(int(done[i])));
                            peak=std::max(peak.load(),level);
                            complete=vad.speech && (ended || stored+EnergyVad::blockSamples>captureCapacity);
                            if (complete) endReason=ended ? 1 : 2;
                        }
                        ++queued;
                        vTaskDelay(pdMS_TO_TICKS(1));
                    }
                    M5.Mic.end(); memset(blocks,0,sizeof(blocks));
                    if(micGuard.load() || !call.enabled.load() || interruptRequested.load()
                        || armEpoch.load()!=captureEpoch || audioEpoch.load()!=turn) {
                        eraseRecording(); state=Idle;
                    } else if(failed) { eraseRecording(); state=Error; errorAt=millis(); }
                    else if(complete && stored) {
                        stored=EnergyVad::retainedSamples(stored,endReason==1);
                        samples=stored;
                        memset(audio+stored,0,(captureCapacity-stored)*sizeof(int16_t));
                        recordCrc=AudioProtocol::crc32(reinterpret_cast<uint8_t*>(audio),stored*2);
                        const auto metrics=CaptureMetrics::measure(audio,stored);
                        lastCompletedSamples=stored; lastCompletedPeak=metrics.peak;
                        lastCompletedClips=metrics.clipped; lastCompletedDc=metrics.dc;
                        lastCompletedRms=uint32_t(sqrt(double(metrics.meanSquare)));
                        lastEndReason=endReason;
                        recordEpoch=turn; ++recordings; completedAt=millis(); state=Ready;
                    } else { eraseRecording(); state=Idle; }
                    applyPendingGuard();
                }
            }
            unlock();
        }
        if(queuedSamples.load() && !streamCount.load() && !streamSpeakerOpen.load() && lock()) {
            const size_t count=queuedSamples.exchange(0);
            if(count && queuedEpoch.load()==audioEpoch.load() && !interruptRequested.load()) playBuffer(incoming,count,true);
            else if(count) { playResult=4; state=restingState(); }
            memset(incoming,0,incomingMaxBytes); unlock();
        }
        if(lock()) {
            if(wipeTurn.exchange(false)) { M5.Mic.end(); eraseRecording(); memset(incoming,0,incomingMaxBytes); state=outputState(); }
            if(micGuard.load()) applyPendingGuard(); else expireRecording();
            if(state==Ready && !samples.load()) state=Idle;
            if(state==Error && !guardLatch.fatal.load() && millis()-errorAt>=1200) state=Idle;
            unlock();
        }
        vTaskDelay(pdMS_TO_TICKS(streamCount.load() ? 1 : 5));
    }
}

void begin() {
    pinMode(1, INPUT_PULLUP);
    pinMode(2, INPUT_PULLUP);
    M5.Mic.end(); M5.Speaker.end();
    // Explicitly quiescent startup does not imply an audio worker exists.
    guardLatch.confirmQuiescent(guardLatch.requested.load(), !M5.Mic.isRunning(), true);
    auto micConfig = M5.Mic.config(); micConfig.sample_rate = 16000; M5.Mic.config(micConfig);
    bootNonce = esp_random();
    if (!bootNonce) bootNonce = 1;
    protocolTestPassed = AudioProtocol::selfTest();
    guardTestPassed = guardLatchSelfTest();
    callTestPassed = callControlSelfTest();
    if (!protocolTestPassed || !guardTestPassed || !callTestPassed) { state = Error; return; }
    audioMutex = xSemaphoreCreateMutex();
    playbackMutex = xSemaphoreCreateMutex();
    audio = static_cast<int16_t*>(ps_calloc(captureCapacity, sizeof(int16_t)));
    incoming = static_cast<int16_t*>(ps_calloc(incomingCapacity, sizeof(int16_t)));
    bool slotsReady=true;
    for (auto& slot : playbackSlots) {
        slot=static_cast<int16_t*>(ps_calloc(StreamPlaybackQueue::maxSamples,sizeof(int16_t)));
        slotsReady=slotsReady && slot;
    }
    if (!audioMutex || !playbackMutex || !audio || !incoming || !slotsReady) {
        state = Error;
    } else {
        guardLatch.fatal = false;
        if (xTaskCreate(task, "dot_voice", 8192, nullptr, 2, nullptr) != pdPASS) {
            guardLatch.fatal = true; state = Error;
        }
    }
    WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true); WiFi.begin();
}
void status() {
    io().printf("VOICE_TEST mic=%u state=%d samples=%u peak=%d recordings=%u blue=%u wifi=%u ip=%s relay=%s last_completed_samples=%u last_completed_peak=%u play_id=%u play_result=%u play_samples=%u play_peak=%u play_count=%u local_plays=%u\n",
        M5.Mic.isRunning(), state.load(), unsigned(samples.load()), peak.load(), unsigned(recordings.load()),
        bluePressed(), WiFi.status() == WL_CONNECTED, WiFi.localIP().toString().c_str(), call.name(),
        unsigned(lastCompletedSamples.load()), unsigned(lastCompletedPeak.load()), unsigned(playId.load()),
        unsigned(playResult.load()), unsigned(playSamples.load()), unsigned(playPeak.load()),
        unsigned(playCount.load()), unsigned(localPlays.load()));
}
const char* listenState() {
    if (CapturePublication::listening(listenRequested,state,Recording)) return "listening";
    if (streamCount.load() || streamSpeakerOpen.load() || state == Playing || state == Queued || state == Receiving) return "playing";
    if (state == Ready && !consumed.load()) return "ready";
    if (call.enabled.load() && recordEpoch.load() == audioEpoch.load()) return "waiting";
    return "idle";
}
bool readScope(const char*& p, uint32_t& epoch) {
    uint32_t boot=0, request=0;
    if (!AudioProtocol::hex8(p,boot) || *p++!=' '
        || !AudioProtocol::decimal(p,request) || *p++!=' '
        || !AudioProtocol::decimal(p,epoch)) return false;
    return boot==bootNonce && request==call.request && epoch==audioEpoch.load();
}
void info() {
    uint32_t bytes = 0, crc = 0, remaining = 0;
    uint32_t reportedQueue=0, reportedAccepted=0, reportedDone=0;
    bool available = false;
    if (lock()) {
        expireRecording();
        if (retained()) {
            const uint32_t age = millis() - completedAt.load();
            remaining = age < retentionMs ? retentionMs - age : 0;
            if (remaining) {
                bytes = samples.load() * 2; crc = recordCrc.load();
                available = !consumed.load() && state == Ready;
            } else eraseRecording();
        }
        unlock();
    }
    // These form one protocol snapshot. Independently reading done then queue
    // while the worker completes could report an empty queue with an old done
    // id, falsely making the host believe accepted audio had been lost.
    if (playbackMutex && xSemaphoreTake(playbackMutex, portMAX_DELAY) == pdTRUE) {
        reportedQueue=streamCount.load();
        reportedAccepted=playAccepted.load();
        reportedDone=playDone.load();
        unlockPlayback();
    }
    io().printf("AUDIO_INFO boot=%08x id=%u bytes=%u crc32=%08x available=%u consumed=%u state=%d expires_ms=%u last_samples=%u last_peak=%u play_id=%u play_result=%u play_samples=%u play_peak=%u play_count=%u mic=%u sync=%u protocol_test=%u guard=%u blue=%u blue_down=%u blue_up=%u blue_qualified=%u blue_ignored=%u blue_high=%u blue_low=%u blue_longest_low_ms=%u blue_mismatch=%u worker=%u fatal=%u guard_pending=%u guard_test=%u call_req=%u call_want=%u call_state=%s call_lease_ms=%u call_test=%u controls=21 audio_epoch=%u record_epoch=%u listen_state=%s volume=%d battery_pct=%d battery_mv=%d battery_charging=%d play_stream=1 play_queue=%u play_slots=3 play_accepted=%u play_done=%u last_clip=%u last_rms=%u last_dc=%d last_end=%u play_burst=%u listen_max_ms=%u listen_silence_ms=%u blue_enabled=%u blue_raw=%u\n",
        bootNonce, unsigned(recordings.load()), bytes, crc, available, consumed.load(), state.load(), remaining,
        unsigned(lastCompletedSamples.load()), unsigned(lastCompletedPeak.load()), unsigned(playId.load()),
        unsigned(playResult.load()), unsigned(playSamples.load()), unsigned(playPeak.load()), unsigned(playCount.load()),
        M5.Mic.isRunning(), io().syncRequired, protocolTestPassed, micGuard.load(), bluePressed(),
        unsigned(blueDown.load()), unsigned(blueUp.load()), unsigned(blueQualified.load()), unsigned(blueIgnored.load()),
        unsigned(blueHighSamples.load()), unsigned(blueLowSamples.load()), unsigned(blueLongestLowMs.load()), unsigned(blueMismatch.load()),
        guardLatch.worker.load(), guardLatch.fatal.load(), guardLatch.pending(), guardTestPassed,
        unsigned(call.request), call.wanted, call.name(), unsigned(call.remaining(millis())), callTestPassed,
        unsigned(audioEpoch.load()), unsigned(recordEpoch.load()), listenState(), volume.load(), batteryPercent, batteryMv, batteryCharging,
        unsigned(reportedQueue), unsigned(reportedAccepted), unsigned(reportedDone),
        unsigned(lastCompletedClips.load()), unsigned(lastCompletedRms.load()), int(lastCompletedDc.load()), unsigned(lastEndReason.load()), unsigned(io().playbackBurst()),
        unsigned(VoiceAudioLimits::captureMs), unsigned(EnergyVad::silenceMs),
        unsigned(blueButtonEnabled), unsigned(digitalRead(1) == LOW));
}
void requireSync() {
    io().syncRequired = true; io().discardAt = io().syncLastAt = millis(); io().syncSize = 0; io().syncOverflow = false;
}
void abortReceive(const char* reason) {
    if (!receiving || !receivingPort) return;
    PortScope scope(*receivingPort);
    receivingPort = nullptr;
    memset(incoming, 0, incomingMaxBytes);
    receiving = false; received = 0; receiveStream = false;
    expireRecording(); state = outputState();
    unlock();
    requireSync();
    io().printf("AUDIO_ERROR %s\n", reason);
}

// Called before normal command parsing; binary and recovery data never reach it.
bool pumpPort(ProtocolPort& port) {
    if (receiving && receivingPort != &port) return false;
    PortScope scope(port);
    if (receiving) {
        // RX owns the mutex, so the main task also enforces retention here.
        expireRecording();
        const uint32_t now = millis();
        if (!io().alive() || now - receiveLastAt >= 2000 || now - receiveAt >= 20000) {
            abortReceive("timeout");
        } else {
            size_t budget = NetworkRxLimits::pumpBytes;
            while (budget && received < receiveExpected) {
                const size_t limit = std::min(NetworkRxLimits::frameBytes,
                    std::min(budget, size_t(receiveExpected) - received));
                const size_t count = io().readSome(receiveStale ? nullptr
                    : reinterpret_cast<uint8_t*>(incoming) + received, limit);
                if (!count) break;
                received += count;
                budget -= count; receiveLastAt = millis();
            }
            if (received == receiveExpected) {
                if (!io().alive() || receiveStale || receiveEpoch != audioEpoch.load() || !call.enabled.load() || micGuard.load()) {
                    memset(incoming,0,incomingMaxBytes);
                    receiving=false; received=0; receivingPort=nullptr; receiveStream=false; state=outputState(); unlock();
                    io().println("AUDIO_ERROR audio_stale");
                } else if (AudioProtocol::crc32(reinterpret_cast<uint8_t*>(incoming), received) != receiveCrc) {
                    abortReceive("crc");
                } else {
                    int level = 0;
                    for (size_t i = 0; i < received / 2; ++i) level = std::max(level, abs(int(incoming[i])));
                    if (receiveStream) {
                        uint32_t accepted=0;
                        xSemaphoreTake(playbackMutex, portMAX_DELAY);
                        const int slot=playbackQueue.freeSlot();
                        if (slot>=0 && io().alive() && streamAllowed(receiveEpoch) && playAccepted.load()!=UINT32_MAX) {
                            memcpy(playbackSlots[slot],incoming,received);
                            const uint32_t next=playAccepted.load()+1;
                            // A blue press may arrive during the short copy.
                            if (io().alive() && streamAllowed(receiveEpoch) && playbackQueue.commit(slot,next,receiveEpoch,received/2,level)) {
                                accepted=next; playAccepted=next; streamCount=playbackQueue.size();
                                if (!streamSpeakerOpen.load()) { playId=next; playSamples=received/2; playPeak=level; playResult=1; }
                            } else memset(playbackSlots[slot],0,streamMaxBytes);
                        }
                        unlockPlayback();
                        memset(incoming,0,incomingMaxBytes);
                        receiving=false; received=0; receivingPort=nullptr; receiveStream=false;
                        state=outputState(); unlock();
                        if (accepted) io().printf("AUDIO_QUEUED id=%u\n",unsigned(accepted));
                        else io().println("AUDIO_ERROR audio_stale");
                    } else {
                        playSamples = received / 2; playPeak = level; playId=++playAccepted; playResult = 1;
                        queuedEpoch=receiveEpoch; queuedSamples = received / 2; state = Queued;
                        receiving = false; received = 0; receivingPort = nullptr;
                        unlock();
                        io().println("AUDIO_QUEUED");
                    }
                }
            }
        }
        return true;
    }
    if (!io().syncRequired) return false;
    // Drain the failed stream until a quiet boundary. Thereafter only the exact
    // boot-bound sync command (or read-only info) is recognized, never wifi etc.
    if (millis() - io().discardAt < 300) {
        bool discarded = false;
        while (io().available()) { io().read(); discarded = true; }
        if (discarded) io().discardAt = millis();
        return true;
    }
    if ((io().syncSize || io().syncOverflow) && millis() - io().syncLastAt >= 300) {
        // A late binary tail without a newline must not poison a later sync.
        memset(io().syncLine, 0, sizeof(io().syncLine)); io().syncSize = 0; io().syncOverflow = false;
    }
    while (io().available()) {
        const int c = io().read();
        if (c < 0) break;
        io().syncLastAt = millis();
        if (c == '\n') {
            io().syncLine[io().syncSize] = 0;
            if (!io().syncOverflow) {
                const char* p = io().syncLine + 11;
                uint32_t nonce = 0;
                if (strncmp(io().syncLine, "audio_sync ", 11) == 0 && AudioProtocol::hex8(p, nonce) && !*p && nonce == bootNonce) {
                    io().syncRequired = false; io().println("AUDIO_SYNC");
                } else if (strcmp(io().syncLine, "audio_info") == 0) info();
                else io().println("AUDIO_ERROR sync_required");
            }
            memset(io().syncLine, 0, sizeof(io().syncLine)); io().syncSize = 0; io().syncOverflow = false;
            if (!io().syncRequired) break;
        } else if (c != '\r') {
            if (io().syncSize < sizeof(io().syncLine) - 1 && c >= 32 && c <= 126) io().syncLine[io().syncSize++] = char(c);
            else io().syncOverflow = true;
        }
    }
    return true;
}

bool command(const char* line, ProtocolPort& port) {
    PortScope scope(port);
    if (!port.alive()) return true;
    const bool voiceCommand = !strncmp(line, "audio_", 6) || !strncmp(line, "call_", 5);
    if (voiceCommand && !port.network() && networkOwner && networkOwner()
        && strcmp(line, "audio_info") && strcmp(line, "audio_guard 1")
        && strncmp(line, "audio_sync ", 11)) {
        io().println("AUDIO_ERROR transport_owner"); return true;
    }
    if (call.expired(millis())) phoneOff();
    if (strcmp(line, "audio_info") == 0) { info(); return true; }
    if (strncmp(line, "call_ack ", 9) == 0) {
        const char* p = line + 9;
        uint32_t boot = 0, request = 0, duration = 0;
        if (!AudioProtocol::hex8(p, boot) || *p++ != ' '
            || !AudioProtocol::decimal(p, request) || *p++ != ' ') {
            io().println("AUDIO_ERROR syntax"); return true;
        }
        CallControl::State next;
        if (!strncmp(p, "off ", 4)) { next = CallControl::Off; p += 4; }
        else if (!strncmp(p, "wait ", 5)) { next = CallControl::Wait; p += 5; }
        else if (!strncmp(p, "ready ", 6)) { next = CallControl::Ready; p += 6; }
        else if (!strncmp(p, "active ", 7)) { next = CallControl::Active; p += 7; }
        else if (!strncmp(p, "error ", 6)) { next = CallControl::Error; p += 6; }
        else { io().println("AUDIO_ERROR syntax"); return true; }
        if (!AudioProtocol::decimal(p, duration) || *p) { io().println("AUDIO_ERROR syntax"); return true; }
        if (boot != bootNonce || !call.accepts(request, next, duration, millis())) {
            io().println("AUDIO_ERROR call_stale"); return true;
        }
        if (next == CallControl::Active) {
            if (!guardLatch.healthy() || guardLatch.pending()) {
                io().println("AUDIO_ERROR guard_pending"); return true;
            }
            if (call.state == CallControl::Active) {
                // A heartbeat must never undo a separate stop request.
                if (!call.enabled.load() || micGuard.load()) {
                    io().println("AUDIO_ERROR call_stopped"); return true;
                }
            } else {
                if (state != Idle || M5.Mic.isRunning() || queuedSamples.load() || streamCount.load()
                    || streamSpeakerOpen.load() || receiving.load()
                    || bluePressed() || !guardLatch.unlockIfReady()) {
                    io().println("AUDIO_ERROR call_not_ready"); return true;
                }
                ++armEpoch;
            }
        } else {
            if (call.enabled.load()) stopPhoneAudio();
            const uint32_t started = millis();
            while (guardLatch.pending() && guardLatch.healthy() && millis() - started < 750) delay(1);
            if (!guardLatch.healthy() || guardLatch.pending() || M5.Mic.isRunning()) {
                io().println("AUDIO_ERROR guard_pending"); return true;
            }
        }
        if (!port.alive()) { phoneOff(); return true; }
        call.acknowledge(next, duration, millis());
        io().printf("CALL_ACK boot=%08x req=%u state=%s\n", bootNonce, unsigned(call.request), call.name());
        return true;
    }
    if (strcmp(line, "audio_guard 1") == 0 || strcmp(line, "audio_guard 0") == 0) {
        const bool locked = line[12] == '1';
        if (locked) {
            if (call.wanted) call.off(millis());
            stopPhoneAudio();
            // An out-of-band USB stop also invalidates queued network bytes.
            // An in-band WSS stop keeps its socket long enough for the ack.
            if (!port.network() && phoneOffHook) phoneOffHook();
            const uint32_t request = guardLatch.requested.load();
            // A request is not a completed lock. Only the audio worker may
            // acknowledge its runtime Mic.end + wipe, including a start that
            // raced with this request. Never clear a fatal initialization error.
            if (!guardLatch.healthy()) { io().println("AUDIO_ERROR guard_fault"); return true; }
            const uint32_t started = millis();
            while (!guardLatch.confirmed(request) && guardLatch.healthy() && millis() - started < 750) delay(1);
            if (!guardLatch.healthy()) { io().println("AUDIO_ERROR guard_fault"); return true; }
            if (!guardLatch.confirmed(request) || M5.Mic.isRunning()) {
                io().println("AUDIO_ERROR guard_pending"); return true;
            }
        } else {
            if (!call.enabled.load() || !call.wanted) { io().println("AUDIO_ERROR call_required"); return true; }
            if (!guardLatch.healthy()) { io().println("AUDIO_ERROR guard_fault"); return true; }
            if (!guardLatch.unlockIfReady()) { io().println("AUDIO_ERROR guard_pending"); return true; }
            ++armEpoch; // Fresh release/press even after a repeated unlock.
        }
        io().printf("AUDIO_GUARD %u\n", locked);
        return true;
    }
    if (strncmp(line, "audio_sync ", 11) == 0) {
        const char* p = line + 11; uint32_t nonce = 0;
        io().println(AudioProtocol::hex8(p, nonce) && !*p && nonce == bootNonce ? "AUDIO_SYNC" : "AUDIO_ERROR sync");
        return true;
    }
    if (strncmp(line, "audio_listen ", 13) == 0) {
        const char* p=line+13; uint32_t epoch=0;
        if (!readScope(p,epoch) || *p) { io().println("AUDIO_ERROR audio_stale"); return true; }
        const AudioCommandScope scope{bootNonce,call.request,epoch};
        if (!call.enabled.load() || !call.wanted || micGuard.load()) { io().println("AUDIO_ERROR call_required"); return true; }
        if (!guardLatch.healthy() || guardLatch.pending()) { io().println("AUDIO_ERROR guard_pending"); return true; }
        if (!lockAudioCommand()) { io().println("AUDIO_ERROR busy"); return true; }
        if (const char* reason=scope.rejection(bootNonce,audioEpoch.load(),call,guardLatch,millis())) {
            unlock(); io().printf("AUDIO_ERROR %s\n",reason); return true;
        }
        if (!port.alive()) { unlock(); return true; }
        if ((state!=Idle && state!=Ready) || M5.Mic.isRunning() || queuedSamples.load()
            || streamCount.load() || streamSpeakerOpen.load() || receiving.load()
            || listenRequested.load() || interruptRequested.load()) {
            unlock(); io().println("AUDIO_ERROR busy"); return true;
        }
        eraseRecording(); consumed=false; wipeTurn=false; state=Idle;
        const uint32_t next=++audioEpoch; ++armEpoch;
        listenRequested=true;
        unlock();
        io().printf("AUDIO_LISTEN epoch=%u\n", unsigned(next));
        return true;
    }
    if (strncmp(line, "audio_get", 9) == 0) {
        uint32_t id=0, epoch=0;
        const char* p=line+10;
        if (line[9]!='2' || *p++!=' ' || !readScope(p,epoch) || *p++!=' '
            || !AudioProtocol::parseId(p,id)) { io().println("AUDIO_ERROR audio_stale"); return true; }
        if (recordEpoch.load()!=epoch) { io().println("AUDIO_ERROR audio_stale"); return true; }
        if (!lock()) { io().println("AUDIO_ERROR busy"); return true; }
        expireRecording();
        if (micGuard.load()) { eraseRecording(); io().println("AUDIO_ERROR guard"); }
        else if (id != recordings.load()) io().println("AUDIO_ERROR id");
        else if (consumed.load()) io().println("AUDIO_ERROR consumed");
        else if (!retained()) io().println("AUDIO_ERROR expired");
        else if (state != Ready) io().println("AUDIO_ERROR busy");
        else {
            // Claim before sending: a dropped connection cannot cause duplication.
            consumed = true; state = Sending;
            const size_t bytes = samples.load() * 2;
            io().printf("AUDIO_DATA %u %u %08x\n", id, unsigned(bytes), unsigned(recordCrc.load()));
            size_t sent = 0; const uint32_t beganAt = millis();
            const uint32_t transferMs = VoiceAudioLimits::transferTimeoutMs(bytes);
            io().bulkOutput = true;
            while (sent < bytes && retained() && millis() - beganAt < transferMs) {
                M5.Touch.update(millis());
                updatePhoneControls();
                if (!io().alive() || !call.enabled.load() || micGuard.load()) break;
                const size_t wrote = io().write(reinterpret_cast<uint8_t*>(audio) + sent, std::min(size_t(1024), bytes - sent));
                sent += wrote;
                if (!wrote) delay(1);
            }
            io().bulkOutput = false;
            // No text trailer: after a partial TX the host must reconnect/resync.
            if (sent != bytes) requireSync();
            expireRecording(); state = restingState();
        }
        unlock(); return true;
    }
    if (strncmp(line, "audio_play", 10) == 0) {
        uint32_t bytes=0, crc=0, epoch=0;
        const bool streaming=line[10]=='3';
        const char* p=line+11;
        if ((!streaming && line[10]!='2') || *p++!=' ' || !readScope(p,epoch) || *p++!=' '
            || !AudioProtocol::parsePlay(p,bytes,crc)) { io().println("AUDIO_ERROR audio_stale"); return true; }
        const AudioCommandScope scope{bootNonce,call.request,epoch};
        if (!bytes || bytes > (streaming ? streamMaxBytes : incomingMaxBytes) || bytes % 2) { io().println("AUDIO_ERROR size"); return true; }
        if (!call.enabled.load() || !call.wanted || micGuard.load()) { io().println("AUDIO_ERROR call_required"); return true; }
        if (!guardLatch.healthy()) { io().println("AUDIO_ERROR guard_fault"); return true; }
        if (guardLatch.pending()) { io().println("AUDIO_ERROR guard_pending"); return true; }
        if (streamFailedEpoch.load()==epoch) { io().println("AUDIO_ERROR playback_failed"); return true; }
        if (!incoming || !(streaming ? lockAudioCommand() : lock())) { io().println("AUDIO_ERROR busy"); return true; }
        if (const char* reason=scope.rejection(bootNonce,audioEpoch.load(),call,guardLatch,millis())) {
            unlock(); io().printf("AUDIO_ERROR %s\n",reason); return true;
        }
        if (!port.alive()) { unlock(); return true; }
        if (streamFailedEpoch.load()==epoch) { unlock(); io().println("AUDIO_ERROR playback_failed"); return true; }
        const bool stateAccepts=state==Idle || state==Ready ||
            (streaming && (streamCount.load() || streamSpeakerOpen.load()));
        if (!stateAccepts || receiving.load() || queuedSamples.load() || M5.Mic.isRunning()
            || listenRequested.load() || interruptRequested.load() || streamCancel.load()
            || (streaming ? streamCount.load()>=StreamPlaybackQueue::slots
                          : (streamCount.load() || streamSpeakerOpen.load()))
            || playAccepted.load()==UINT32_MAX) {
            unlock(); io().println("AUDIO_ERROR busy"); return true;
        }
        expireRecording();
        memset(incoming, 0, incomingMaxBytes);
        receiveEpoch=epoch; receiveStale=false; receiveStream=streaming;
        receiveExpected = bytes; receiveCrc = crc; received = 0;
        receiveAt = receiveLastAt = millis(); receiving = true; receivingPort = &port; state = Receiving;
        io().println("AUDIO_READY");
        // Mutex intentionally held by main-loop task until RX complete/aborted.
        return true;
    }
    if (strncmp(line, "audio_", 6) == 0) { io().println("AUDIO_ERROR command"); return true; }
    return false;
}
// Called on the main loop only. Read display metadata, never PCM or audio locks.
DotActivity::Input activityInput() {
    DotActivity::Input result;
    result.wanted = call.wanted;
    result.active = call.enabled.load() && !micGuard.load();
    result.healthy = guardLatch.healthy();
    // Preserve the capture publication order across request -> Recording.
    result.listenRequested = listenRequested.load();
    const int visualState = state.load();
    result.error = call.state == CallControl::Error || visualState == Error;
    result.recording = visualState == Recording;
    // Queued/Receiving alone do not mean sound is being played yet.
    result.speaking = streamSpeakerOpen.load() || visualState == Playing;
    return result;
}
void overlay(M5Canvas& canvas, DotActivity::Mode activity) {
    const int s = state.load();
    const bool active = call.enabled.load();
    const bool stopping = !active && (guardLatch.pending() || M5.Mic.isRunning()
        || s == Playing || queuedSamples.load() || streamCount.load() || streamSpeakerOpen.load());
    const char* label = !guardLatch.healthy() || s == Error ? "音声エラー" : stopping ? "停止中"
        : !call.wanted ? (activity == DotActivity::Mode::Ending ? "またね" : "音声 OFF")
        : call.state == CallControl::Error ? "Mac準備待ち"
        : !active ? "接続待ち" : activity == DotActivity::Mode::Listening ? "聞いています"
        : activity == DotActivity::Mode::Speaking ? "お話し中" : "考えています";
    const char* hint = !call.wanted ? "ONで接続を依頼" : !active ? "Macで通話を準備"
        : activity == DotActivity::Mode::Listening ? "そのまま話しかけてね" : "上下スワイプで音量";
    const uint16_t green = canvas.color565(35, 180, 110);
    const uint16_t red = canvas.color565(219, 66, 83);
    canvas.setTextDatum(middle_center); canvas.setTextSize(1);
    canvas.fillRoundRect(108, 33, 250, 73, 18, TFT_BLACK);
    canvas.setFont(&fonts::Font2); canvas.setTextColor(0xBDF7);
    canvas.drawString("dot", 233, 52);
    const bool wifiConnected = WiFi.status() == WL_CONNECTED;
    canvas.fillRoundRect(80,57,100,27,8,TFT_BLACK);
    canvas.fillCircle(94,71,4,wifiConnected ? green : red);
    canvas.setTextDatum(middle_left);
    canvas.setTextColor(wifiConnected ? green : 0xBDF7);
    canvas.drawString(wifiConnected ? "Wi-Fi" : "Wi-Fi --",105,71);
    // Keep all battery pixels inside the circular display's upper-right rim.
    const uint16_t batteryColor=batteryPercent>=0 && batteryPercent<=20 ? red : 0xBDF7;
    canvas.fillRoundRect(288,57,100,27,8,TFT_BLACK);
    canvas.drawRoundRect(299,65,27,13,2,batteryColor);
    canvas.fillRect(326,69,3,5,batteryColor);
    if(batteryPercent>=0) canvas.fillRect(302,68,std::max(0,std::min(21,batteryPercent*21/100)),7,batteryColor);
    canvas.setTextColor(batteryColor); canvas.setTextDatum(middle_left);
    char batteryLabel[16];
    if(batteryPercent>=0) snprintf(batteryLabel,sizeof(batteryLabel),"%d%%%s",batteryPercent,batteryCharging==1?"+":"");
    else snprintf(batteryLabel,sizeof(batteryLabel),"--%%");
    canvas.drawString(batteryLabel,333,71);
    canvas.setTextDatum(middle_center);
    canvas.setFont(&fonts::efontJA_24);
    canvas.setTextColor(active ? green : call.wanted ? 0xFE49 : 0xBDF7);
    canvas.drawString(label, 233, 84);
    // Opaque controls stay readable when the IMU sends Dot towards the rim.
    canvas.fillRect(0, 368, 466, 98, TFT_BLACK);
    canvas.fillRoundRect(115, 374, 110, 56, 25, green);
    canvas.fillRoundRect(241, 374, 110, 56, 25, red);
    // Handset silhouettes drawn locally; no font/emoji or downloaded asset.
    for (int i = 0; i < 5; ++i) {
        canvas.drawLine(134+i, 388, 140+i, 402, TFT_WHITE);
        canvas.drawLine(140+i, 402, 154+i, 410, TFT_WHITE);
    }
    canvas.fillRoundRect(131, 385, 12, 10, 3, TFT_WHITE);
    canvas.fillRoundRect(151, 404, 12, 10, 3, TFT_WHITE);
    canvas.fillEllipse(271, 398, 19, 9, TFT_WHITE);
    canvas.fillEllipse(271, 404, 13, 8, red);
    canvas.fillRoundRect(252, 397, 10, 12, 3, TFT_WHITE);
    canvas.fillRoundRect(280, 397, 10, 12, 3, TFT_WHITE);
    canvas.setFont(&fonts::Font4); canvas.setTextColor(TFT_WHITE);
    canvas.drawString("ON", 190, 402); canvas.drawString("OFF", 318, 402);
    canvas.setFont(&fonts::efontJA_16); canvas.setTextColor(0xBDF7);
    if(volumeAt.load() && millis()-volumeAt.load()<2200) {
        char text[32]; snprintf(text,sizeof(text),"音量 %d%%",(volume.load()*100+127)/255);
        canvas.drawString(text,233,447);
        canvas.fillRoundRect(398,160,12,148,6,0x4208);
        const int filled=volume.load()*144/255;
        if (filled) canvas.fillRoundRect(400,306-filled,8,filled,4,TFT_WHITE);
    } else canvas.drawString(hint, 233, 447);
}
}
