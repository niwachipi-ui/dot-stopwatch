#pragma once
#include <stdint.h>

// Arduino-ESP32 2.0.17 does not enable MBEDTLS_HAVE_TIME_DATE. These checks
// supplement (never replace) the SDK's CA-chain and hostname verification.
namespace WebSocketsCertificateTime {
struct UtcDate {
    int year, mon, day, hour, min, sec;
};

inline bool reasonableClock(int64_t unixSeconds) {
    // This firmware is released in 2026. A boot-time clock must not make old
    // certificates valid; fail closed again if the clock jumps implausibly far.
    return unixSeconds >= INT64_C(1767225600) &&
           unixSeconds < INT64_C(4102444800); // [2026-01-01, 2100-01-01)
}

template <typename Date>
inline bool validDate(const Date& date) {
    if (date.year < 1 || date.year > 9999 || date.mon < 1 || date.mon > 12 ||
        date.hour < 0 || date.hour > 23 || date.min < 0 || date.min > 59 ||
        date.sec < 0 || date.sec > 59) return false;
    static const int days[] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
    int daysInMonth = days[date.mon - 1];
    if (date.mon == 2 && date.year % 4 == 0 &&
        (date.year % 100 != 0 || date.year % 400 == 0)) ++daysInMonth;
    return date.day >= 1 && date.day <= daysInMonth;
}

template <typename Left, typename Right>
inline int compare(const Left& left, const Right& right) {
    const int a[] = {left.year, left.mon, left.day, left.hour, left.min, left.sec};
    const int b[] = {right.year, right.mon, right.day, right.hour, right.min, right.sec};
    for (unsigned i = 0; i < 6; ++i) {
        if (a[i] < b[i]) return -1;
        if (a[i] > b[i]) return 1;
    }
    return 0;
}

template <typename Certificate>
inline bool chainValidAt(const Certificate* certificate, const UtcDate& now) {
    if (!certificate || !validDate(now)) return false;
    unsigned count = 0;
    for (; certificate; certificate = certificate->next) {
        // Also bound malformed/cyclic chains; normal public chains are short.
        if (++count > 16 || !validDate(certificate->valid_from) ||
            !validDate(certificate->valid_to) ||
            compare(certificate->valid_from, certificate->valid_to) > 0 ||
            compare(now, certificate->valid_from) < 0 ||
            compare(now, certificate->valid_to) > 0) return false;
    }
    return true;
}
} // namespace WebSocketsCertificateTime
