#pragma once
#include <stdint.h>
#include <stddef.h>
#include <algorithm>
// Portable state machines: no GPIO, microphone, or remote action in this file.
struct HoldButton {
    enum Event { None, Short, Volume };
    bool initialized=false, raw=false, stable=false, armed=false, tracking=false, held=false;
    uint32_t rawAt=0, releaseAt=0, pressAt=0, repeatAt=0;
    Event update(bool pressed, uint32_t now) {
        if (!initialized) { initialized=true; raw=stable=pressed; rawAt=releaseAt=now; return None; }
        if (pressed != raw) { raw=pressed; rawAt=now; }
        if (raw != stable && uint32_t(now-rawAt)>=35) {
            stable=raw;
            if (!stable) {
                releaseAt=now;
                // The audio worker may miss the hold threshold while blocked.
                // Missing a Volume event must never turn a long hold into Short.
                const uint32_t duration=now-pressAt;
                const bool shortPress=tracking && !held && duration>=50 && duration<700;
                tracking=false; held=false;
                if (shortPress) return Short;
            } else if (armed) {
                armed=false; tracking=true; held=false; pressAt=repeatAt=now;
            }
        }
        // A noisy/highly asserted input cannot manufacture fresh presses.
        if (!raw && !stable && uint32_t(now-releaseAt)>=500 && uint32_t(now-rawAt)>=500) armed=true;
        if (raw && stable && tracking && uint32_t(now-pressAt)>=700
            && (!held || uint32_t(now-repeatAt)>=200)) {
            held=true; repeatAt=now; return Volume;
        }
        return None;
    }
};
struct VoiceAudioLimits {
    static constexpr uint32_t sampleRate=16000, captureMs=60000;
    static constexpr size_t captureSamples=size_t(sampleRate)*(captureMs/1000);
    // Incoming replies keep the existing ten-second non-streaming limit.
    static constexpr size_t incomingSamples=size_t(sampleRate)*10;
    static uint32_t transferTimeoutMs(size_t bytes) {
        // Allow 100 KiB/s plus startup time for a full clip. This stays below
        // the 30-second call lease; OFF and disconnect are checked during TX.
        const uint64_t boundedBytes=std::min(bytes,captureSamples*sizeof(int16_t));
        const uint32_t needed=1000+uint32_t((boundedBytes*1000+102399)/102400);
        return std::min(uint32_t(20000),std::max(uint32_t(5000),needed));
    }
};
struct EnergyVad {
    static constexpr size_t blockSamples=320, preRoll=3200;
    static constexpr unsigned blockMs=20, silenceMs=1600, silenceBlocks=silenceMs/blockMs;
    static_assert(VoiceAudioLimits::captureSamples % blockSamples == 0, "Capture ends on a whole PCM block");
    static_assert(silenceMs % blockMs == 0, "Silence duration uses whole PCM blocks");
    // Decide the endpoint with the full 1.6 s pause. Once confirmed, don't
    // replay all of that quiet tail into Chrome: retain 400 ms after speech.
    static size_t retainedSamples(size_t captured, bool silenceEnded) {
        constexpr size_t quietSamples = VoiceAudioLimits::sampleRate*silenceMs/1000;
        constexpr size_t keepSamples = VoiceAudioLimits::sampleRate*400/1000;
        return silenceEnded && captured >= quietSamples+6*blockSamples
            ? captured-(quietSamples-keepSamples) : captured;
    }
    unsigned candidate=0, silent=0, voiced=0;
    int noise=70;
    bool speech=false;
    // Each call represents 20 ms of completed mono 16 kHz PCM16.
    bool update(const int16_t* data) {
        uint32_t sum=0; int peak=0;
        for(size_t i=0;i<blockSamples;++i) {
            const int value=data[i]<0 ? -int(data[i]) : int(data[i]);
            sum+=value; peak=std::max(peak,value);
        }
        const int mean=int(sum/blockSamples);
        // Keep the existing onset gate. Once speech starts, quieter syllables
        // and word endings can continue it without becoming false silence.
        const bool active=speech ? mean>=std::max(150,noise*2) && peak>=500
                                 : mean>=std::max(220,noise*3) && peak>=800;
        if (!speech) {
            if (active) ++candidate;
            else { candidate=0; noise=(noise*31+std::min(mean,500))/32; }
            if (candidate>=6) { speech=true; voiced=candidate; silent=0; }
        } else if(active) { ++voiced; silent=0; }
        else ++silent;
        return speech && voiced>=6 && silent>=silenceBlocks;
    }
};
