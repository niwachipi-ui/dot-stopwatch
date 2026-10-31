#pragma once
#include <atomic>
#include <Preferences.h>
#include <time.h>
#include <WebSocketsClient.h>
#include <WebSocketsCertificateTime.h>
#include "protocol_port.h"
#include "bounded_ring.h"
#include "network_rx_limits.h"
#include "network_diagnostics.h"
#include "pairing_config.h"
#include "wifi_root_ca.h"

namespace NetworkBridge {
constexpr size_t frameLimit = NetworkRxLimits::frameBytes;
constexpr char version[] = "wifi-voice-28";
constexpr char protocol[] = "dotwatch.v1";
Pairing pairing = {};
portMUX_TYPE mux = portMUX_INITIALIZER_UNLOCKED;
BoundedRing rx, tx;
std::atomic<uint32_t> generation{1}, accepted{0}, configRevision{1};
std::atomic<bool> connected{false}, configured{false}, workerReady{false}, closeWanted{false};
enum Fault { None, MissingCA, Allocation, Disconnected, Overflow, BadFrame, BadHandshake, Reconfigured };
std::atomic<int> fault{None};
NetworkDiagnostics::State diagnostics;
bool caReady() { return strstr(DOT_WIFI_ROOT_CA, "-----BEGIN CERTIFICATE-----") != nullptr; }
void invalidate(bool up, Fault why, NetworkDiagnostics::Reason reason = NetworkDiagnostics::Unknown,
                bool disconnectEvent = false) {
    portENTER_CRITICAL(&mux);
    const uint32_t now = millis();
    if (disconnectEvent) reason = diagnostics.disconnected(reason, now);
    if (why == Overflow) ++diagnostics.overflows;
    if (why == BadFrame) ++diagnostics.badFrames;
    diagnostics.record(why, reason, generation.load(), now,
        connected.load() && accepted.load() == generation.load(), rx.used, tx.used);
    // A disconnect event has consumed the pending cause, including one just
    // classified above. Do not carry it into later failed reconnect attempts.
    if (disconnectEvent) diagnostics.pendingCloseReason = NetworkDiagnostics::Unknown;
    if (up) diagnostics.connected();
    connected = false; accepted = 0;
    rx.clear(); tx.clear(); ++generation;
    fault = why; connected = up;
    portEXIT_CRITICAL(&mux);
}
void requestClose() {
    invalidate(false, Disconnected, NetworkDiagnostics::LocalClose);
    closeWanted = true; // Only the TLS task calls disconnect/free on its socket.
}
class Port : public ProtocolPort {
public:
    bool network() const override { return true; }
    bool alive() const override { return connected.load() && accepted.load() == generation.load(); }
    bool playbackBurst() const override {
        // Storage is bound before the worker starts and is never resized.
        return rx.data && NetworkRxLimits::supportsBurst(rx.capacity) && workerReady.load() && alive();
    }
    int available() override {
        portENTER_CRITICAL(&mux); int n = alive() ? rx.used : 0; portEXIT_CRITICAL(&mux); return n;
    }
    int peek() override {
        portENTER_CRITICAL(&mux); int n = alive() && rx.used ? rx.data[rx.head] : -1; portEXIT_CRITICAL(&mux); return n;
    }
    int read() override {
        uint8_t byte = 0;
        portENTER_CRITICAL(&mux); bool ok = alive() && rx.pop(&byte, 1) == 1; portEXIT_CRITICAL(&mux);
        return ok ? byte : -1;
    }
    size_t readSome(uint8_t* data, size_t count) override {
        // Bound each critical section even when a full chunk is buffered.
        portENTER_CRITICAL(&mux);
        const size_t size = alive() ? rx.pop(data, std::min(count, frameLimit)) : 0;
        portEXIT_CRITICAL(&mux);
        return size;
    }
    void flush() override {}
    size_t write(uint8_t byte) override { return write(&byte, 1); }
    size_t write(const uint8_t* data, size_t count) override {
        portENTER_CRITICAL(&mux);
        // Metadata writes must be complete. Only the PCM loop handles partial
        // writes/backpressure; truncating an AUDIO_DATA header corrupts framing.
        const bool overflow = alive() && !bulkOutput && count > tx.free();
        const size_t size = alive() && !overflow ? std::min(count, tx.free()) : 0;
        if (size) tx.push(data, size);
        if (overflow) diagnostics.record(Overflow, NetworkDiagnostics::TxOverflow,
            generation.load(), millis(), alive(), rx.used, tx.used);
        portEXIT_CRITICAL(&mux);
        if (overflow) { invalidate(false, Overflow, NetworkDiagnostics::TxOverflow); closeWanted = true; }
        return size;
    }
} port;

// Use the library's framing/TLS implementation, but bound HTTP lines and require
// an explicit, exact subprotocol response. The stock client retains the offered
// protocol when the server omits the response header, so that alone is not proof.
class StrictClient : public WebSocketsClient {
    char header[1025] = {};
    size_t used = 0, total = 0;
    bool headerPhase = false, selected = false;
public:
    StrictClient() {
        // The upstream client is normally global; this instance is task-local.
        // Its optional mTLS pointers have no in-class initializers in 2.7.2.
        _client_cert = nullptr; _client_key = nullptr;
        _lastConnectionFail = 0; _lastHeaderSent = 0;
    }
    void loop() {
        if (_client.status == WSC_HEADER && headerPhase) {
            if (!_client.tcp || !_client.tcp->connected()
                || millis() - _lastHeaderSent >= WEBSOCKETS_TCP_TIMEOUT) {
                clientDisconnect(&_client, "header_timeout"); headerPhase = false; return;
            }
            size_t budget = 4096;
            while (budget-- && _client.status == WSC_HEADER && _client.tcp->available()) {
                const int value = _client.tcp->read();
                if (value < 0) break;
                if (++total > 8192 || (value != '\n' && used == sizeof(header) - 1)) {
                    clientDisconnect(&_client, "header_size"); headerPhase = false; return;
                }
                if (value == '\n') {
                    header[used] = 0;
                    String line(header); line.trim();
                    const int colon = line.indexOf(':');
                    if (colon > 0 && line.substring(0, colon).equalsIgnoreCase("Sec-WebSocket-Protocol")) {
                        String value = line.substring(colon + 1); value.trim();
                        if (selected || value != protocol) { clientDisconnect(&_client, "subprotocol"); headerPhase = false; return; }
                        selected = true;
                    }
                    if (colon > 0 && line.substring(0, colon).equalsIgnoreCase("Sec-WebSocket-Extensions")) {
                        String extensions = line.substring(colon + 1); extensions.trim();
                        if (extensions.length()) { clientDisconnect(&_client, "extensions"); headerPhase = false; return; }
                    }
                    if (!line.length() && !selected) { clientDisconnect(&_client, "subprotocol_missing"); headerPhase = false; return; }
                    handleHeader(&_client, &line);
                    memset(header, 0, sizeof(header)); used = 0;
                } else header[used++] = char(value);
            }
            if (_client.status != WSC_HEADER) headerPhase = false;
            return;
        }
        if (_client.status == WSC_NOT_CONNECTED) {
            headerPhase = selected = false; used = total = 0;
            memset(header, 0, sizeof(header)); _client.cProtocol = protocol;
        }
        WebSocketsClient::loop();
        if (_client.status == WSC_HEADER && !headerPhase) {
            headerPhase = true; selected = false; used = total = 0;
            memset(header, 0, sizeof(header)); _client.cProtocol = "";
        }
    }
};

void task(void*) {
    StrictClient ws;
    uint32_t revision = 0;
    bool enabled = false;
    bool timeSyncStarted = false;
    uint8_t block[frameLimit] = {};
    ws.onEvent([&](WStype_t type, uint8_t* data, size_t size) {
        if (type == WStype_CONNECTED) { invalidate(true, None); return; }
        if (type == WStype_DISCONNECTED) {
            invalidate(false, Disconnected, NetworkDiagnostics::disconnectReason(data, size), true); return;
        }
        if (type == WStype_PING || type == WStype_PONG) return;
        if (type != WStype_BIN || !size || size > frameLimit) {
            invalidate(false, BadFrame, NetworkDiagnostics::InvalidFrame); closeWanted = true; return;
        }
        portENTER_CRITICAL(&mux);
        const bool ok = connected.load() && rx.push(data, size);
        if (!ok) diagnostics.record(Overflow, NetworkDiagnostics::RxOverflow,
            generation.load(), millis(), port.alive(), rx.used, tx.used);
        portEXIT_CRITICAL(&mux);
        if (!ok) { invalidate(false, Overflow, NetworkDiagnostics::RxOverflow); closeWanted = true; }
    });
    workerReady = true;
    for (;;) {
        if (closeWanted.exchange(false)) ws.disconnect();
        const uint32_t latest = configRevision.load();
        if (latest != revision) {
            ws.disconnect(); enabled = false;
            Pairing copy;
            portENTER_CRITICAL(&mux); copy = pairing; portEXIT_CRITICAL(&mux);
            revision = latest;
            if (validPair(copy) && caReady()) {
                ws.beginSslWithCA(copy.host, 443, "/dot-watch/v1", DOT_WIFI_ROOT_CA, protocol);
                ws.setExtraHeaders(""); // Suppress the library's default Origin: file://.
                char authorization[72] = "Bearer ";
                memcpy(authorization + 7, copy.token, 65);
                ws.setAuthorization(authorization);
                memset(authorization, 0, sizeof(authorization));
                ws.setReconnectInterval(3000);
                ws.enableHeartbeat(5000, 3000, 1);
                enabled = true;
            } else if (configured.load()) fault = MissingCA;
            memset(&copy, 0, sizeof(copy));
        }
        if (enabled && WiFi.status() == WL_CONNECTED) {
            if (!timeSyncStarted) {
                // The TLS adapter rejects an unsynchronized clock before
                // sending credentials. SNTP runs asynchronously.
                configTime(0, 0, "time.cloudflare.com", "time.google.com");
                timeSyncStarted = true;
            }
            ws.loop();
            if (connected.load() && !closeWanted.load()) {
                uint32_t epoch;
                portENTER_CRITICAL(&mux);
                epoch = generation.load();
                const size_t size = port.alive() ? tx.pop(block, sizeof(block)) : 0;
                portEXIT_CRITICAL(&mux);
                if (size && generation.load() == epoch && connected.load()) {
                    if (!ws.sendBIN(block, size)) { invalidate(false, Disconnected, NetworkDiagnostics::TxFailure); closeWanted = true; }
                }
                memset(block, 0, sizeof(block));
            }
        } else if (connected.load()) { invalidate(false, Disconnected, NetworkDiagnostics::WifiLost); ws.disconnect(); }
        vTaskDelay(pdMS_TO_TICKS(2));
    }
}
void begin() {
    auto* storage = static_cast<uint8_t*>(ps_calloc(NetworkRxLimits::rxBytes + NetworkRxLimits::txBytes, 1));
    if (!storage) { fault = Allocation; return; }
    rx.bind(storage, NetworkRxLimits::rxBytes);
    tx.bind(storage + NetworkRxLimits::rxBytes, NetworkRxLimits::txBytes);
    Preferences prefs;
    if (prefs.begin("dot-net", true)) {
        if (prefs.getBytesLength("pair") == sizeof(pairing)) prefs.getBytes("pair", &pairing, sizeof(pairing));
        prefs.end();
    }
    if (!validPair(pairing)) memset(&pairing, 0, sizeof(pairing));
    configured = validPair(pairing);
    if (xTaskCreate(task, "dot_wss", 12288, nullptr, 1, nullptr) != pdPASS) fault = Allocation;
}
void info() {
    NetworkDiagnostics::State snapshot;
    portENTER_CRITICAL(&mux); snapshot = diagnostics; portEXIT_CRITICAL(&mux);
    Serial.printf("NET_INFO version=%s configured=%u ca_ready=%u connected=%u generation=%u fault=%d worker=%u clock_ready=%u first_fault=%u first_reason=%u first_gen=%u first_at_ms=%u first_rx=%u first_tx=%u overflows=%u badframes=%u disconnects=%u last_close_reason=%u last_close_at_ms=%u\n",
        version, configured.load(), caReady(), connected.load(), unsigned(generation.load()), fault.load(), workerReady.load(),
        WebSocketsCertificateTime::reasonableClock(static_cast<int64_t>(time(nullptr))),
        unsigned(snapshot.firstFault), unsigned(snapshot.firstReason), unsigned(snapshot.firstGeneration), unsigned(snapshot.firstAt),
        unsigned(snapshot.firstRx), unsigned(snapshot.firstTx), unsigned(snapshot.overflows), unsigned(snapshot.badFrames),
        unsigned(snapshot.disconnects), unsigned(snapshot.lastCloseReason), unsigned(snapshot.lastCloseAt));
}
bool configure(const char* host, const char* token) {
    Pairing value = {}; value.magic = pairingMagic;
    if (!validHost(host) || !validToken(token)) return false;
    memcpy(value.host, host, strlen(host)); memcpy(value.token, token, 64);
    Preferences prefs;
    if (!prefs.begin("dot-net", false)) { memset(&value, 0, sizeof(value)); return false; }
    const bool stored = prefs.putBytes("pair", &value, sizeof(value)) == sizeof(value);
    prefs.end();
    if (stored) {
        requestClose();
        portENTER_CRITICAL(&mux); pairing = value; configured = true; ++configRevision; portEXIT_CRITICAL(&mux);
    }
    memset(&value, 0, sizeof(value)); return stored;
}
bool clear() {
    Preferences prefs;
    if (!prefs.begin("dot-net", false)) return false;
    const bool cleared = !prefs.isKey("pair") || prefs.remove("pair"); prefs.end();
    if (cleared) {
        requestClose();
        portENTER_CRITICAL(&mux); memset(&pairing, 0, sizeof(pairing)); configured = false; ++configRevision; portEXIT_CRITICAL(&mux);
    }
    return cleared;
}
}
