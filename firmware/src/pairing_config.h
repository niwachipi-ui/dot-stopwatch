#pragma once
#include <stdint.h>
#include <string.h>
namespace NetworkBridge {
struct Pairing { uint32_t magic; char host[254]; char token[65]; };
constexpr uint32_t pairingMagic = 0x44575031;
bool validHost(const char* host) {
    const size_t len = strlen(host);
    if (!len || len > 253) return false;
    size_t label = 0;
    for (size_t i = 0; i < len; ++i) {
        const char c = host[i];
        if (c == '.') { if (!label || host[i-1] == '-') return false; label = 0; continue; }
        if (!((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-')) return false;
        if ((!label && c == '-') || ++label > 63) return false;
    }
    return label && host[len-1] != '-';
}
bool validToken(const char* token) {
    if (strlen(token) != 64) return false;
    for (size_t i = 0; i < 64; ++i)
        if (!((token[i] >= '0' && token[i] <= '9') || (token[i] >= 'a' && token[i] <= 'f'))) return false;
    return true;
}
bool validPair(const Pairing& value) {
    return value.magic == pairingMagic && value.host[253] == 0 && value.token[64] == 0
        && validHost(value.host) && validToken(value.token);
}
}
