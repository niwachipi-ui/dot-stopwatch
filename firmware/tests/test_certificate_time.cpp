#include <cassert>
#include <cstdio>
#include "../lib/WebSockets/src/WebSocketsCertificateTime.h"

using namespace WebSocketsCertificateTime;
struct Certificate {
    UtcDate valid_from, valid_to;
    const Certificate* next;
};

int main() {
    assert(!reasonableClock(-1));
    assert(!reasonableClock(0));
    assert(!reasonableClock(1767225599LL));
    assert(reasonableClock(1767225600LL));
    assert(reasonableClock(4102444799LL));
    assert(!reasonableClock(4102444800LL));
    assert(!reasonableClock(INT64_MAX));

    assert(validDate(UtcDate{2000, 2, 29, 0, 0, 0}));
    assert(validDate(UtcDate{2028, 2, 29, 23, 59, 59}));
    assert(!validDate(UtcDate{2100, 2, 29, 0, 0, 0}));
    assert(!validDate(UtcDate{2026, 2, 29, 0, 0, 0}));
    assert(!validDate(UtcDate{2026, 4, 31, 0, 0, 0}));
    assert(!validDate(UtcDate{0, 1, 1, 0, 0, 0}));
    assert(!validDate(UtcDate{10000, 1, 1, 0, 0, 0}));
    assert(!validDate(UtcDate{2026, 0, 1, 0, 0, 0}));
    assert(!validDate(UtcDate{2026, 13, 1, 0, 0, 0}));
    assert(!validDate(UtcDate{2026, 1, 0, 0, 0, 0}));
    assert(!validDate(UtcDate{2026, 1, 1, 24, 0, 0}));
    assert(!validDate(UtcDate{2026, 1, 1, 0, 60, 0}));
    assert(!validDate(UtcDate{2026, 1, 1, 0, 0, 60}));
    assert(!validDate(UtcDate{2026, 1, 1, 0, 0, -1}));

    const UtcDate now{2026, 10, 10, 4, 0, 0};
    Certificate intermediate{{2025, 1, 1, 0, 0, 0}, {2030, 1, 1, 0, 0, 0}, nullptr};
    Certificate leaf{{2026, 10, 10, 3, 59, 59}, {2026, 10, 10, 4, 0, 1}, &intermediate};
    assert(chainValidAt(&leaf, now));
    assert(chainValidAt(&leaf, leaf.valid_from));
    assert(chainValidAt(&leaf, leaf.valid_to));
    assert(!chainValidAt(&leaf, UtcDate{2026, 10, 10, 3, 59, 58}));
    assert(!chainValidAt(&leaf, UtcDate{2026, 10, 10, 4, 0, 2}));
    assert(!chainValidAt<Certificate>(nullptr, now));
    assert(!chainValidAt(&leaf, UtcDate{2026, 10, 10, 24, 0, 0}));

    intermediate.valid_to = {2026, 10, 10, 3, 59, 59};
    assert(!chainValidAt(&leaf, now)); // A valid leaf cannot hide an expired intermediate.
    intermediate.valid_from = {2027, 1, 1, 0, 0, 0};
    intermediate.valid_to = {2030, 1, 1, 0, 0, 0};
    assert(!chainValidAt(&leaf, now)); // Nor a future-dated intermediate.
    intermediate.valid_from = {2025, 1, 1, 0, 0, 0};
    assert(chainValidAt(&leaf, now));
    leaf.valid_from = {2026, 10, 10, 4, 0, 2};
    assert(!chainValidAt(&leaf, now)); // Reversed interval.
    leaf.valid_from = {2026, 10, 10, 3, 59, 59};
    leaf.valid_to = {2026, 10, 32, 0, 0, 0};
    assert(!chainValidAt(&leaf, now));
    leaf.valid_to = {2026, 10, 10, 4, 0, 1};
    intermediate.next = &leaf;
    assert(!chainValidAt(&leaf, now)); // Bounded even for a cyclic chain.
    std::puts("certificate clock, civil dates, validity boundaries and full-chain checks passed");
}
