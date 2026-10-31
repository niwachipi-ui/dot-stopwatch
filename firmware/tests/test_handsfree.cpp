#include <cassert>
#include <cstdio>
#include <vector>
#include "../src/handsfree_controls.h"
#include "../src/stream_playback_queue.h"
static HoldButton::Event run(HoldButton& b, bool down, uint32_t start, uint32_t end) {
    auto found=HoldButton::None;
    for(uint32_t t=start;t<end;++t) { auto e=b.update(down,t); if(e!=HoldButton::None) found=e; }
    return found;
}
// Sparse calls model the real audio worker pausing between button samples.
// Relative times also run across uint32_t millis() wraparound.
static void armedPress(HoldButton& b, uint32_t base) {
    assert(b.update(false,base)==HoldButton::None);
    assert(b.update(false,base+500)==HoldButton::None);
    assert(b.update(true,base+600)==HoldButton::None);
    assert(b.update(true,base+635)==HoldButton::None);
}
static void buttonSamplingRegressions(uint32_t base) {
    HoldButton stalled;
    armedPress(stalled,base);
    assert(stalled.update(true,base+1285)==HoldButton::None); // 650 ms since debounced press.
    // No sample while still down after 700 ms: no Volume event was emitted.
    assert(stalled.update(false,base+1350)==HoldButton::None);
    assert(stalled.update(false,base+1385)==HoldButton::None); // Must not become Short.

    for (uint32_t duration : {49u,50u,699u,700u,701u,5000u}) {
        HoldButton boundary;
        armedPress(boundary,base);
        assert(boundary.update(false,base+600+duration)==HoldButton::None);
        const auto expected=duration>=50 && duration<700 ? HoldButton::Short : HoldButton::None;
        assert(boundary.update(false,base+635+duration)==expected);
    }

    HoldButton hold;
    armedPress(hold,base);
    assert(hold.update(true,base+1334)==HoldButton::None); // 699 ms.
    assert(hold.update(true,base+1335)==HoldButton::Volume); // 700 ms.
    assert(hold.update(true,base+1534)==HoldButton::None);
    assert(hold.update(true,base+1535)==HoldButton::Volume); // 200 ms repeat.
    assert(hold.update(false,base+1536)==HoldButton::None);
    assert(hold.update(false,base+1571)==HoldButton::None); // Release after hold never interrupts.
}
int main() {
    buttonSamplingRegressions(0);
    buttonSamplingRegressions(UINT32_MAX-800);
    HoldButton b;
    assert(run(b,true,0,4000)==HoldButton::None); // Boot-held input never acts.
    assert(run(b,false,4000,4600)==HoldButton::None);
    assert(run(b,true,4600,4750)==HoldButton::None);
    assert(run(b,false,4750,4800)==HoldButton::Short);
    assert(run(b,false,4800,5350)==HoldButton::None);
    assert(run(b,true,5350,6200)==HoldButton::Volume);
    assert(run(b,false,6200,6300)==HoldButton::None); // Long never emits short.
    HoldButton jitter;
    for(uint32_t t=0;t<3000;++t) assert(jitter.update(t%20<10,t)==HoldButton::None);
    int16_t quiet[320]={}, voice[320], softVoice[320], noise[320], click[320]={};
    std::fill(voice,voice+320,1200); click[0]=20000;
    std::fill(noise,noise+320,120);
    for(size_t i=0;i<320;++i) softVoice[i]=i%5==0 ? 600 : 100;
    EnergyVad vad;
    for(int i=0;i<2000;++i) assert(!vad.update(quiet));
    assert(!vad.speech);
    assert(!vad.update(click)); assert(!vad.update(quiet)); assert(!vad.speech);
    for(int i=0;i<5;++i) { assert(!vad.update(voice)); assert(!vad.speech); }
    assert(!vad.update(voice)); assert(vad.speech);
    // A short breath or a thinking pause must remain inside the same utterance.
    for(unsigned pauseBlocks : {35u,60u}) {
        for(unsigned i=0;i<pauseBlocks;++i) assert(!vad.update(quiet));
        assert(vad.speech);
        assert(!vad.update(voice));
        assert(vad.silent==0);
    }
    for(unsigned i=0;i<79;++i) assert(!vad.update(quiet));
    assert(vad.update(quiet)); // Only 1.6 seconds of silence ends the utterance.

    EnergyVad softOnset;
    for(unsigned i=0;i<200;++i) assert(!softOnset.update(softVoice));
    assert(!softOnset.speech); // Hysteresis never makes weak noise start capture.
    EnergyVad quietEnding;
    for(unsigned i=0;i<6;++i) assert(!quietEnding.update(voice));
    for(unsigned i=0;i<200;++i) assert(!quietEnding.update(softVoice));
    assert(quietEnding.speech && quietEnding.silent==0); // Quieter syllables continue speech.
    for(unsigned i=0;i<79;++i) assert(!quietEnding.update(noise));
    assert(quietEnding.update(noise)); // Low background noise still counts as silence.

    // Keep endpoint detection unchanged; remove only 1.2 s of its confirmed
    // quiet tail from the clip sent to Chrome. Retain 400 ms after the last
    // voiced block, including any soft ending below the onset threshold.
    std::vector<int16_t> utterance(12*320,1200);
    utterance.insert(utterance.end(),10*320,600);
    const size_t speechEnd=utterance.size();
    utterance.insert(utterance.end(),80*320,0);
    const size_t kept=EnergyVad::retainedSamples(utterance.size(),true);
    assert(utterance.size()-kept==19200);
    assert(kept==speechEnd+6400);
    assert(utterance[speechEnd-1]==600);
    assert(EnergyVad::retainedSamples(utterance.size(),false)==utterance.size()); // Capacity cutoff.
    assert(EnergyVad::retainedSamples(100,true)==100); // No underflow for malformed short clips.
    assert(EnergyVad::retainedSamples(0,true)==0);

    static_assert(VoiceAudioLimits::captureSamples==960000, "Sixty-second capture bound");
    static_assert(VoiceAudioLimits::incomingSamples==160000, "Playback capacity must not expand");
    static_assert(VoiceAudioLimits::captureSamples*sizeof(int16_t)==1920000, "Capture byte budget");
    assert(VoiceAudioLimits::transferTimeoutMs(320000)==5000);
    assert(VoiceAudioLimits::transferTimeoutMs(1920000)==19750);
    assert(VoiceAudioLimits::transferTimeoutMs(size_t(-1))==19750); // Oversized input cannot overflow or extend TX.
    EnergyVad sustained;
    for(size_t captured=0;captured<VoiceAudioLimits::captureSamples;captured+=EnergyVad::blockSamples) {
        assert(!sustained.update(voice));
        if(captured>=160000) assert(sustained.speech); // Continues beyond the old ten-second cutoff.
    }
    assert(sustained.speech && sustained.voiced==3000);

    // Full-budget diagnostics must not overflow 32-bit sums at maximum PCM16 levels.
    std::vector<int16_t> full(VoiceAudioLimits::captureSamples);
    for(size_t i=0;i<full.size();++i) full[i]=i%2 ? -32768 : 32767;
    const auto metrics=CaptureMetrics::measure(full.data(),full.size());
    assert(metrics.peak==32768 && metrics.clipped==960000 && metrics.dc==0);
    assert(metrics.meanSquare==1073709056u);
    std::puts("button controls, 0.7/1.2-second pauses, 1.6-second endpoint, soft endings, 60-second capture and full-budget metrics passed");
}
