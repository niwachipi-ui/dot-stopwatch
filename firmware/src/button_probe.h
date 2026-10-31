#pragma once
#include <esp_wifi.h>
#include <soc/io_mux_reg.h>

// Guarded, temporary isolation of input noise. Does not record, change pin
// routing, store credentials, or alter persistent Wi-Fi configuration.
namespace ButtonProbe {
bool active = false, radioStopped = false, oldAutoReconnect = false, oldConnected = false;
uint8_t phase = 0;
uint32_t phaseAt = 0, downAt = 0, upAt = 0, highAt = 0, lowAt = 0, mismatchAt = 0;
uint32_t muxAt = 0, outputAt = 0, enableAt = 0;
const char* const labels[] = {"baseline", "no_redraw", "no_imu", "no_m5_update", "radio_stopped", "restored"};
bool skipDraw() { return active && phase == 1; }
bool skipImu() { return active && phase == 2; }
bool skipUpdate() { return active && phase == 3; }
void restoreRadio() {
    if (radioStopped) {
        esp_wifi_start();
        WiFi.setAutoReconnect(oldAutoReconnect);
        if (oldAutoReconnect || oldConnected) esp_wifi_connect();
        radioStopped = false;
    }
}
void baseline() {
    phaseAt = millis(); downAt = VoiceTest::blueDown.load(); upAt = VoiceTest::blueUp.load();
    highAt = VoiceTest::blueHighSamples.load(); lowAt = VoiceTest::blueLowSamples.load();
    mismatchAt = VoiceTest::blueMismatch.load(); VoiceTest::blueLongestLowMs = 0;
    muxAt = REG_READ(IO_MUX_GPIO1_REG); outputAt = REG_READ(GPIO_FUNC1_OUT_SEL_CFG_REG);
    enableAt = (REG_READ(GPIO_ENABLE_REG) >> 1) & 1;
}
void finish(const char* result) {
    restoreRadio(); active = false;
    Serial.printf("BUTTON_PROBE_DONE result=%s guard=%u mic=%u\n", result, VoiceTest::micGuard.load(), M5.Mic.isRunning());
}
void update() {
    if (!active) return;
    if (!VoiceTest::micGuard.load() || M5.Mic.isRunning()) { finish("unsafe_state"); return; }
    if (millis() - phaseAt < 3000) return;
    const uint32_t elapsed = millis() - phaseAt;
    Serial.printf("BUTTON_PROBE phase=%s ms=%u down=%u up=%u high=%u low=%u longest_low_ms=%u mismatch=%u mux_start=%08x mux_end=%08x out_start=%08x out_end=%08x oe_start=%u oe_end=%u radio_stopped=%u\n",
        labels[phase], elapsed, unsigned(VoiceTest::blueDown.load() - downAt), unsigned(VoiceTest::blueUp.load() - upAt),
        unsigned(VoiceTest::blueHighSamples.load() - highAt), unsigned(VoiceTest::blueLowSamples.load() - lowAt),
        std::min(elapsed, VoiceTest::blueLongestLowMs.load()), unsigned(VoiceTest::blueMismatch.load() - mismatchAt),
        muxAt, unsigned(REG_READ(IO_MUX_GPIO1_REG)), outputAt, unsigned(REG_READ(GPIO_FUNC1_OUT_SEL_CFG_REG)),
        enableAt, unsigned((REG_READ(GPIO_ENABLE_REG) >> 1) & 1), radioStopped);
    if (phase == 5) { finish("ok"); return; }
    ++phase;
    if (phase == 4 && WiFi.getMode() != WIFI_OFF) {
        oldAutoReconnect = WiFi.getAutoReconnect(); oldConnected = WiFi.status() == WL_CONNECTED;
        WiFi.setAutoReconnect(false);
        radioStopped = esp_wifi_stop() == ESP_OK;
        if (!radioStopped) WiFi.setAutoReconnect(oldAutoReconnect);
    } else if (phase == 5) restoreRadio();
    baseline();
}
bool command(const char* text) {
    if (strcmp(text, "button_probe") == 0) {
        if (active || !VoiceTest::micGuard.load() || M5.Mic.isRunning()
            || VoiceTest::state != VoiceTest::Idle || VoiceTest::receiving) {
            Serial.println("BUTTON_PROBE_ERROR busy_or_unlocked"); return true;
        }
        phase = 0; active = true; baseline(); Serial.println("BUTTON_PROBE_STARTED"); return true;
    }
    if (strcmp(text, "button_probe_abort") == 0) {
        if (active) finish("aborted"); else Serial.println("BUTTON_PROBE_DONE result=idle");
        return true;
    }
    if (active && (strcmp(text, "audio_guard 0") == 0 || strncmp(text, "audio_play", 10) == 0
        || strncmp(text, "wifi\t", 5) == 0 || strcmp(text, "capture") == 0)) {
        Serial.println("AUDIO_ERROR probe"); return true;
    }
    return false;
}
}
