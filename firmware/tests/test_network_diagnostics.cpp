#include <cassert>
#include <cstdio>
#include <string>
#include "../src/network_diagnostics.h"

int main() {
    using namespace NetworkDiagnostics;
    State state;
    // Failed pre-auth/startup attempts may be counted, but must not consume the
    // useful first-live-failure slot. No remote string is retained.
    state.record(3, HeaderTimeout, 1, 1500, false, 0, 0);
    assert(state.firstFault == 0);
    assert(state.disconnected(Unknown, 1501) == HeaderTimeout);
    assert(state.disconnects == 1 && state.lastCloseAt == 1501);
    state.connected();
    assert(state.pendingCloseReason == Unknown && state.firstFault == 0);

    // This runs under the same critical section as the failed ring push,
    // before invalidation can reset accepted/generation or wipe ring contents.
    state.record(4, RxOverflow, 7, 10000, true, 65536, 321);
    assert(state.firstFault == 4 && state.firstReason == RxOverflow);
    assert(state.firstGeneration == 7 && state.firstAt == 10000);
    assert(state.firstRx == 65536 && state.firstTx == 321);
    assert(state.disconnected(Unknown, 10001) == RxOverflow);
    state.record(3, Unknown, 8, 10002, false, 0, 0);
    assert(state.firstFault == 4 && state.firstReason == RxOverflow);
    assert(state.pendingCloseReason == Unknown);
    assert(state.disconnected(SocketCleanup, 10003) == SocketCleanup);

    // Reconnection and a later independent failure preserve the original
    // timestamp/scope while the last-close metadata and count keep advancing.
    state.connected();
    state.record(3, SocketLost, 10, 20000, true, 22, 33);
    assert(state.disconnected(SocketLost, 20001) == SocketLost);
    assert(state.firstFault == 4 && state.firstReason == RxOverflow);
    assert(state.firstAt == 10000 && state.firstGeneration == 7 && state.firstRx == 65536);
    assert(state.lastCloseReason == SocketLost && state.lastCloseAt == 20001 && state.disconnects == 4);

    State stopped;
    stopped.connected();
    stopped.record(3, LocalClose, 3, 99, true, 0, 0);
    assert(stopped.firstFault == 0);
    assert(stopped.disconnected(Unknown, 100) == LocalClose);
    stopped.connected();
    stopped.record(5, InvalidFrame, 5, 0xfffffff0u, true, 1, 2);
    assert(stopped.firstFault == 5 && stopped.firstAt == 0xfffffff0u);
    stopped.disconnected(Unknown, 10);
    assert(stopped.firstAt == 0xfffffff0u && stopped.lastCloseAt == 10);

    auto classify = [](const std::string& text) {
        return disconnectReason(reinterpret_cast<const uint8_t*>(text.data()), text.size());
    };
    assert(classify("Connection lost") == SocketLost);
    assert(classify("TCP connection cleanup") == SocketCleanup);
    assert(classify("Header response timeout") == HeaderTimeout);
    assert(classify("header_timeout") == HeaderTimeout);
    assert(classify("header_size") == HeaderSize);
    assert(classify("subprotocol") == Subprotocol);
    assert(classify("subprotocol_missing") == Subprotocol);
    assert(classify("extensions") == Extensions);
    assert(classify("subprotocol unexpected peer text") == Unknown);
    assert(classify(std::string("Connection lost\0extra", 21)) == Unknown);
    assert(disconnectReason(nullptr, 0) == Unknown);
    assert(disconnectReason(nullptr, 4096) == Unknown);
    assert(classify(std::string(4096, 'x')) == Unknown);
    std::puts("PASS: first live failure retained through disconnect/reconnect, pre-live/local excluded, exact numeric close reasons");
}
