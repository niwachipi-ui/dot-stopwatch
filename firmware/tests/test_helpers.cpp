#include <cassert>
#include <cstring>
#include <deque>
#include <string>
#include <cstdio>
#include "../src/bounded_ring.h"
#include "../src/pairing_config.h"
#include "../src/call_control.h"
#include "../src/guard_latch.h"
#include "../src/audio_protocol.h"
int main() {
    assert(callControlSelfTest() && guardLatchSelfTest() && AudioProtocol::selfTest());
    using namespace NetworkBridge;
    for (auto host : {"dot.example.com", "a", "a-1.example", "123.example"}) assert(validHost(host));
    for (auto host : {"", "-a.example", "a-.example", "a..example", ".example", "example.",
                      "https://example", "example:443", "EXAMPLE.com", "a\r\nX: x", "a b", "a/b"}) assert(!validHost(host));
    assert(validHost((std::string(63, 'a') + "." + std::string(63, 'a') + "." + std::string(63, 'a') + "." + std::string(61, 'a')).c_str()));
    assert(!validHost((std::string(64, 'a') + ".com").c_str()));
    assert(!validHost((std::string(63, 'a') + "." + std::string(63, 'a') + "." + std::string(63, 'a') + "." + std::string(62, 'a')).c_str()));
    assert(validToken(std::string(64, 'a').c_str()));
    assert(!validToken(std::string(63, 'a').c_str()) && !validToken(std::string(65, 'a').c_str()));
    assert(!validToken(std::string(64, 'A').c_str()) && !validToken(std::string(64, 'g').c_str()));
    Pairing pair{}; pair.magic=pairingMagic;
    std::strcpy(pair.host, "dot.example.com"); std::memset(pair.token, 'a', 64);
    assert(validPair(pair)); pair.token[64]='a'; assert(!validPair(pair));
    pair.token[64]=0; pair.host[253]='a'; assert(!validPair(pair));
    uint8_t storage[37], input[67], output[67];
    BoundedRing ring; ring.bind(storage, sizeof(storage));
    for (auto byte : storage) assert(byte==0);
    std::deque<uint8_t> model; uint32_t rng=0x82938a2;
    auto random = [&] { rng = rng*1664525u+1013904223u; return rng; };
    for (unsigned step=0; step<100000; ++step) {
        const size_t count=random()%sizeof(input);
        switch(random()%3) {
        case 0: {
            for (size_t i=0;i<count;++i) input[i]=uint8_t(random());
            const bool expected=count<=sizeof(storage)-model.size();
            uint8_t before[sizeof(storage)]; std::memcpy(before,storage,sizeof(storage));
            assert(ring.push(input,count)==expected);
            if(expected) for(size_t i=0;i<count;++i) model.push_back(input[i]);
            else assert(!std::memcmp(before,storage,sizeof(storage)));
            break;
        }
        case 1: {
            const size_t head=ring.head, expected=std::min(count,model.size());
            assert(ring.pop(output,count)==expected);
            for(size_t i=0;i<expected;++i) {
                assert(output[i]==model.front()); model.pop_front();
                assert(storage[(head+i)%sizeof(storage)]==0);
            }
            break;
        }
        case 2:
            ring.clear(); model.clear();
            for(auto byte:storage) assert(byte==0);
            break;
        }
        assert(ring.used==model.size() && ring.free()+ring.used==sizeof(storage));
    }
    std::puts("PASS: 100000 ring/model transitions, zeroization, provisioning bounds, real guard/call/protocol regressions");
}
