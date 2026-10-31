#pragma once
#include <stdint.h>
#include <stddef.h>
#include <string.h>

// Standard CRC-32/ISO-HDLC, identical to Python zlib.crc32.
namespace AudioProtocol {
inline uint32_t crc32(const uint8_t* bytes, size_t count) {
    uint32_t crc = 0xFFFFFFFFu;
    for (size_t i = 0; i < count; ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xEDB88320u & (0u - (crc & 1u)));
    }
    return crc ^ 0xFFFFFFFFu;
}
inline bool decimal(const char*& p, uint32_t& result) {
    if (*p < '0' || *p > '9') return false;
    result = 0;
    while (*p >= '0' && *p <= '9') {
        const uint32_t digit = unsigned(*p++ - '0');
        if (result > (UINT32_MAX - digit) / 10u) return false;
        result = result * 10u + digit;
    }
    return true;
}
inline bool hex8(const char*& p, uint32_t& result) {
    result = 0;
    for (unsigned i = 0; i < 8; ++i) {
        const char c = *p;
        unsigned digit;
        if (c >= '0' && c <= '9') digit = unsigned(c - '0');
        else if (c >= 'a' && c <= 'f') digit = unsigned(c - 'a') + 10;
        else if (c >= 'A' && c <= 'F') digit = unsigned(c - 'A') + 10;
        else return false;
        ++p;
        result = (result << 4) | digit;
    }
    return true;
}
inline bool parseId(const char* p, uint32_t& id) {
    return decimal(p, id) && *p == 0;
}
inline bool parsePlay(const char* p, uint32_t& bytes, uint32_t& crc) {
    if (!decimal(p, bytes) || *p++ != ' ') return false;
    return hex8(p, crc) && *p == 0;
}
inline bool selfTest() {
    if (crc32(reinterpret_cast<const uint8_t*>("123456789"), 9) != 0xCBF43926u
        || crc32(nullptr, 0) != 0) return false;
    uint32_t value = 0, crc = 0;
    if (!parseId("4294967295", value) || value != UINT32_MAX
        || parseId("4294967296", value) || parseId("-1", value)
        || parseId("", value) || parseId("1x", value)) return false;
    if (!parsePlay("320000 deadBEEF", value, crc) || value != 320000 || crc != 0xDEADBEEFu
        || parsePlay("320000 deadbeef0", value, crc)
        || parsePlay("320000 deadbee", value, crc)
        || parsePlay("320000  deadbeef", value, crc)
        || parsePlay("320000 deadbeef tail", value, crc)) return false;
    return true;
}
}
