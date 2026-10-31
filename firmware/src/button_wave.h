#pragma once
#include <esp_timer.h>
#include <soc/rtc_io_reg.h>

// Read-only simultaneous blue/yellow input sampling in the timer task. No GPIO
// mode/pull/output writes and no microphone operation occur in this diagnostic.
namespace ButtonWave {
constexpr uint32_t periodUs = 100, durationUs = 1000000;
constexpr uint32_t limits[] = {100, 500, 1000, 5000, 20000, 100000, 500000};
struct PinStats {
    uint32_t high = 0, low = 0, edges = 0, maxHighUs = 0, maxLowUs = 0;
    uint32_t lowRuns[8] = {}, highRuns[8] = {}, edgeAt[24] = {};
    uint8_t edgeLevel[24] = {};
    uint32_t runAt = 0, muxStart = 0, muxLast = 0, rtcStart = 0, rtcLast = 0;
    uint32_t muxChanges = 0, rtcChanges = 0;
    bool last = false;
};
struct Stats {
    PinStats pins[2];
    uint32_t samples = 0, firstUs = 0, lastUs = 0;
    uint32_t minGap = UINT32_MAX, maxGap = 0, late = 0, shortGap = 0;
    uint32_t states[4] = {}, simultaneousEdges = 0, outEnableStart = 0, outEnableLast = 0, outEnableChanges = 0;
    uint8_t previous = 0;
    bool aborted = false;
} stats;
esp_timer_handle_t timer = nullptr;
std::atomic<bool> active{false}, done{false};

void closeRun(PinStats& pin, uint32_t now) {
    const uint32_t length = now - pin.runAt;
    unsigned bucket = 0;
    while (bucket < 7 && length >= limits[bucket]) ++bucket;
    if (pin.last) { ++pin.highRuns[bucket]; pin.maxHighUs = std::max(pin.maxHighUs, length); }
    else { ++pin.lowRuns[bucket]; pin.maxLowUs = std::max(pin.maxLowUs, length); }
}
void sample(void*) {
    if (done.load()) return;
    if (!VoiceTest::micGuard.load() || M5.Mic.isRunning()) {
        stats.aborted = true; done = true; return;
    }
    const uint32_t now = uint32_t(esp_timer_get_time());
    // Single register read: the two reference inputs describe the same instant.
    const uint8_t bits = uint8_t((REG_READ(GPIO_IN_REG) >> 1) & 3u);
    const uint32_t oe = (REG_READ(GPIO_ENABLE_REG) >> 1) & 3u;
    if (!stats.samples) {
        stats.firstUs = now; stats.previous = bits;
        stats.outEnableStart = stats.outEnableLast = oe;
    } else {
        const uint32_t gap = now - stats.lastUs;
        stats.minGap = std::min(stats.minGap, gap); stats.maxGap = std::max(stats.maxGap, gap);
        if (gap > 250) ++stats.late;
        if (gap < 50) ++stats.shortGap;
        if ((bits ^ stats.previous) == 3) ++stats.simultaneousEdges;
        if (oe != stats.outEnableLast) { ++stats.outEnableChanges; stats.outEnableLast = oe; }
    }
    ++stats.states[bits];
    for (unsigned i = 0; i < 2; ++i) {
        auto& pin = stats.pins[i];
        const bool high = (bits >> i) & 1u;
        const uint32_t mux = REG_READ(i ? IO_MUX_GPIO2_REG : IO_MUX_GPIO1_REG);
        const uint32_t rtc = REG_READ(i ? RTC_IO_TOUCH_PAD2_REG : RTC_IO_TOUCH_PAD1_REG);
        if (!stats.samples) {
            pin.last = high; pin.runAt = now;
            pin.muxStart = pin.muxLast = mux; pin.rtcStart = pin.rtcLast = rtc;
        } else {
            if (mux != pin.muxLast) { ++pin.muxChanges; pin.muxLast = mux; }
            if (rtc != pin.rtcLast) { ++pin.rtcChanges; pin.rtcLast = rtc; }
            if (high != pin.last) {
                closeRun(pin, now);
                if (pin.edges < 24) { pin.edgeAt[pin.edges] = now - stats.firstUs; pin.edgeLevel[pin.edges] = high; }
                ++pin.edges; pin.last = high; pin.runAt = now;
            }
        }
        if (high) ++pin.high; else ++pin.low;
    }
    stats.previous = bits; stats.lastUs = now; ++stats.samples;
    if (now - stats.firstUs >= durationUs || stats.samples >= 15000) {
        for (auto& pin : stats.pins) closeRun(pin, now);
        done = true;
    }
}
void printBuckets(const char* label, const uint32_t* values) {
    Serial.printf(" %s=", label);
    for (unsigned i = 0; i < 8; ++i) Serial.printf(i ? ",%u" : "%u", values[i]);
}
void update() {
    if (!active.load() || !done.load()) return;
    esp_timer_stop(timer); esp_timer_delete(timer); timer = nullptr;
    const uint32_t elapsed = stats.samples ? stats.lastUs - stats.firstUs : 0;
    Serial.printf("BUTTON_WAVE samples=%u elapsed_us=%u nominal_us=%u min_gap_us=%u max_gap_us=%u late_over250=%u short_under50=%u both_low=%u blue_high_yellow_low=%u blue_low_yellow_high=%u both_high=%u simultaneous_edges=%u oe_start=%u oe_end=%u oe_changes=%u\n",
        stats.samples, elapsed, periodUs, stats.minGap == UINT32_MAX ? 0 : stats.minGap, stats.maxGap,
        stats.late, stats.shortGap, stats.states[0], stats.states[1], stats.states[2], stats.states[3],
        stats.simultaneousEdges, stats.outEnableStart, stats.outEnableLast, stats.outEnableChanges);
    Serial.println("BUTTON_WAVE_BINS us=0:100,100:500,500:1000,1000:5000,5000:20000,20000:100000,100000:500000,500000:inf endpoints_censored=1");
    for (unsigned i = 0; i < 2; ++i) {
        const auto& p = stats.pins[i];
        Serial.printf("BUTTON_WAVE_PIN pin=%u high=%u low=%u edges=%u longest_high_us=%u longest_low_us=%u mux_start=%08x mux_end=%08x mux_changes=%u rtc_start=%08x rtc_end=%08x rtc_changes=%u rtc_mux_start=%u rtc_mux_end=%u",
            i + 1, p.high, p.low, p.edges, p.maxHighUs, p.maxLowUs, p.muxStart, p.muxLast, p.muxChanges,
            p.rtcStart, p.rtcLast, p.rtcChanges, (p.rtcStart >> 19) & 1u, (p.rtcLast >> 19) & 1u);
        printBuckets("low_runs", p.lowRuns); printBuckets("high_runs", p.highRuns); Serial.println();
        Serial.printf("BUTTON_WAVE_EDGES pin=%u first_us_level=", i + 1);
        for (unsigned e = 0; e < std::min(uint32_t(24), p.edges); ++e)
            Serial.printf(e ? ",%u:%u" : "%u:%u", p.edgeAt[e], p.edgeLevel[e]);
        Serial.println();
    }
    active = false;
    Serial.printf("BUTTON_WAVE_DONE result=%s guard=%u mic=%u\n", stats.aborted ? "aborted" : "ok", VoiceTest::micGuard.load(), M5.Mic.isRunning());
}
bool command(const char* line) {
    if (strcmp(line, "button_wave") == 0) {
        if (active.load() || ButtonProbe::active || !VoiceTest::micGuard.load() || M5.Mic.isRunning()
            || VoiceTest::state != VoiceTest::Idle || VoiceTest::receiving) {
            Serial.println("BUTTON_WAVE_ERROR busy_or_unlocked"); return true;
        }
        stats = Stats{}; done = false;
        esp_timer_create_args_t args = {};
        args.callback = sample; args.dispatch_method = ESP_TIMER_TASK;
        args.name = "dot_button_wave"; args.skip_unhandled_events = true;
        if (esp_timer_create(&args, &timer) != ESP_OK) { Serial.println("BUTTON_WAVE_ERROR timer"); return true; }
        active = true;
        if (esp_timer_start_periodic(timer, periodUs) != ESP_OK) {
            active = false; esp_timer_delete(timer); timer = nullptr;
            Serial.println("BUTTON_WAVE_ERROR timer"); return true;
        }
        Serial.println("BUTTON_WAVE_STARTED"); return true;
    }
    if (active.load() && (strcmp(line, "audio_guard 0") == 0 || strncmp(line, "audio_play", 10) == 0
        || strncmp(line, "wifi\t", 5) == 0 || strcmp(line, "capture") == 0 || strcmp(line, "button_probe") == 0)) {
        Serial.println("AUDIO_ERROR wave"); return true;
    }
    return false;
}
}
