#pragma once
#include <Arduino.h>
#include "network_rx_limits.h"
// Only the main task touches parser state. Native TLS lives on another task.
class ProtocolPort : public Stream {
public:
    bool syncRequired = false, syncOverflow = false, bulkOutput = false;
    uint32_t discardAt = 0, syncLastAt = 0;
    char syncLine[48] = {};
    size_t syncSize = 0;
    char commandLine[NetworkRxLimits::commandBytes] = {};
    size_t commandSize = 0;
    bool commandOverflow = false;
    virtual bool network() const = 0;
    virtual bool alive() const = 0;
    virtual bool playbackBurst() const { return false; }
    // Consume only immediately available bytes, never Stream's timed read.
    // A null destination drains a known stale binary payload.
    virtual size_t readSome(uint8_t* dst, size_t count) {
        size_t readCount = 0;
        while (readCount < count && available()) {
            const int byte = read();
            if (byte < 0) break;
            if (dst) dst[readCount] = uint8_t(byte);
            ++readCount;
        }
        return readCount;
    }
    void resetParser() {
        syncRequired = syncOverflow = commandOverflow = bulkOutput = false;
        discardAt = syncLastAt = 0; syncSize = commandSize = 0;
        memset(syncLine, 0, sizeof(syncLine));
        memset(commandLine, 0, sizeof(commandLine));
    }
};
class USBProtocolPort : public ProtocolPort {
public:
    bool network() const override { return false; }
    bool alive() const override { return true; }
    int available() override { return Serial.available(); }
    int read() override { return Serial.read(); }
    int peek() override { return Serial.peek(); }
    void flush() override {}
    size_t write(uint8_t byte) override { return Serial.write(byte); }
    size_t write(const uint8_t* data, size_t count) override { return Serial.write(data, count); }
};
USBProtocolPort usbProtocol;
