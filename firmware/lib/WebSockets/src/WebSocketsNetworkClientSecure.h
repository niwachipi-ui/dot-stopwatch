#pragma once
#include <WiFiClientSecure.h>
#include <time.h>
#include "WebSocketsCertificateTime.h"
// The library's documented NETWORK_CUSTOM hook keeps TLS setup bounded without
// changing the Arduino SDK or sharing a native TLS object across tasks.
using WebSocketsNetworkClient = WiFiClient;
class WebSocketsNetworkClientSecure : public WiFiClientSecure {
public:
    WebSocketsNetworkClientSecure() { setHandshakeTimeout(5); }
    int setTimeout(uint32_t milliseconds) override {
        // WebSockets passes milliseconds, while ESP32's WiFiClient uses seconds.
        return WiFiClientSecure::setTimeout((milliseconds + 999) / 1000);
    }
    int connect(const char* host, uint16_t port, int32_t timeout) override {
        // Match the SDK's timeout overload, then use the guarded host overload.
        _timeout = timeout;
        return connect(host, port);
    }
    int connect(const char* host, uint16_t port) override {
        if (!_CA_cert || !*_CA_cert || _use_insecure || _pskIdent || _psKey ||
            !host || !*host || !clockReady()) {
            stop();
            return 0;
        }
        if (!WiFiClientSecure::connect(host, port)) return 0;

        // Native connect already verified the CA chain and hostname. Check all
        // supplied certificates before WebSockets may send HTTP Authorization.
        // Read time again because the NTP clock can change during the handshake.
        const time_t seconds = time(nullptr);
        struct tm utc = {};
        if (!WebSocketsCertificateTime::reasonableClock(static_cast<int64_t>(seconds)) ||
            !gmtime_r(&seconds, &utc) ||
            !WebSocketsCertificateTime::chainValidAt(getPeerCertificate(),
                WebSocketsCertificateTime::UtcDate{utc.tm_year + 1900, utc.tm_mon + 1,
                    utc.tm_mday, utc.tm_hour, utc.tm_min, utc.tm_sec})) {
            stop();
            return 0;
        }
        return 1;
    }
private:
    static bool clockReady() {
        return WebSocketsCertificateTime::reasonableClock(
            static_cast<int64_t>(time(nullptr)));
    }
};
