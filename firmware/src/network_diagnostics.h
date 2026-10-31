#pragma once
#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Metadata only. The owner serializes every update/snapshot with the network
// mutex. No remote text, credentials or PCM are retained here.
namespace NetworkDiagnostics {
enum Reason : uint32_t {
    Unknown, RxOverflow, TxOverflow, InvalidFrame, SocketLost, SocketCleanup,
    HeaderTimeout, HeaderSize, Subprotocol, Extensions, TxFailure, WifiLost,
    LocalClose
};
inline bool matches(const uint8_t* data, size_t size, const char* expected) {
    return data && size == strlen(expected) && !memcmp(data, expected, size);
}
inline Reason disconnectReason(const uint8_t* data, size_t size) {
    if (matches(data, size, "Connection lost")) return SocketLost;
    if (matches(data, size, "TCP connection cleanup")) return SocketCleanup;
    if (matches(data, size, "Header response timeout") || matches(data, size, "header_timeout")) return HeaderTimeout;
    if (matches(data, size, "header_size")) return HeaderSize;
    if (matches(data, size, "subprotocol") || matches(data, size, "subprotocol_missing")) return Subprotocol;
    if (matches(data, size, "extensions")) return Extensions;
    return Unknown;
}
struct State {
    uint32_t firstFault = 0, firstReason = Unknown, firstGeneration = 0, firstAt = 0;
    uint32_t firstRx = 0, firstTx = 0;
    uint32_t overflows = 0, badFrames = 0, disconnects = 0;
    uint32_t lastCloseReason = Unknown, lastCloseAt = 0;
    Reason pendingCloseReason = Unknown;

    // Connection success only resets the transient hint, not the first fault.
    void connected() { pendingCloseReason = Unknown; }
    void record(uint32_t fault, Reason reason, uint32_t generation, uint32_t now,
                bool wasLive, size_t rxUsed, size_t txUsed) {
        if (!fault) return;
        if (reason != Unknown && pendingCloseReason == Unknown) pendingCloseReason = reason;
        // A rejected initial handshake or deliberate shutdown isn't a failure
        // of a live authenticated session. Retries must not replace its cause.
        if (firstFault || !wasLive || reason == LocalClose) return;
        firstFault = fault; firstReason = reason;
        firstGeneration = generation; firstAt = now;
        firstRx = rxUsed; firstTx = txUsed;
    }
    Reason disconnected(Reason reason, uint32_t now) {
        ++disconnects;
        if (pendingCloseReason != Unknown) reason = pendingCloseReason;
        lastCloseReason = reason; lastCloseAt = now;
        pendingCloseReason = Unknown;
        return reason;
    }
};
}
