#include <cassert>
#include <cmath>
#include <cstdio>
#include <cstring>
#include "../src/dot_activity.h"

using namespace DotActivity;

static void expect(Frame frame, Mode mode) {
    assert(frame.mode == mode);
    assert(std::isfinite(frame.weight) && frame.weight >= 0 && frame.weight <= 1);
}
static bool near(float a, float b) { return std::fabs(a - b) < 0.0001f; }

static void bootAndPriority() {
    Controller boot;
    Input off;
    expect(boot.update(off, 0), Mode::Idle);
    auto idle = boot.update(off, 9999);
    assert(idle.weight == 0 && idle.ageMs == 9999);
    // Stale hardware flags and even an unavailable worker never manufacture a
    // call animation or microphone indication while the user's intent is OFF.
    off.healthy = false; off.error = off.recording = off.listenRequested = off.speaking = true;
    expect(boot.update(off, 10000), Mode::Idle);

    // All flag combinations validate priority and the strict Speaking source.
    for (unsigned bits = 0; bits < 128; ++bits) {
        Controller controller;
        Input in;
        in.wanted = bits & 1; in.active = bits & 2; in.healthy = bits & 4;
        in.error = bits & 8; in.recording = bits & 16;
        in.listenRequested = bits & 32; in.speaking = bits & 64;
        const auto f = controller.update(in, 500);
        if (!in.wanted) expect(f, Mode::Idle);
        else if (!in.healthy || in.error) expect(f, Mode::Unavailable);
        else if (!in.active) expect(f, Mode::Connecting);
        else if (in.speaking) expect(f, Mode::Speaking);
        else if (in.recording || in.listenRequested) expect(f, Mode::Listening);
        else expect(f, Mode::Thinking);
        if (!in.speaking) assert(f.mode != Mode::Speaking);
        if (!in.active) assert(f.mode != Mode::Listening && f.mode != Mode::Speaking);
        assert(in.wanted == bool(bits & 1) && in.active == bool(bits & 2));
    }
}

static void transitions() {
    Controller controller;
    Input in;
    controller.update(in, 100);
    in.wanted = true;
    auto f = controller.update(in, 200);
    expect(f, Mode::Connecting); assert(f.weight == 0 && f.ageMs == 0);
    f = controller.update(in, 350);
    assert(near(f.weight, .5f) && f.ageMs == 150);
    f = controller.update(in, 500);
    assert(f.weight == 1 && f.ageMs == 300);
    in.active = true; in.listenRequested = true;
    f = controller.update(in, 510);
    expect(f, Mode::Listening); assert(f.weight == 1 && f.ageMs == 0);
    // Handover from pending listen to recording does not restart the pose.
    in.listenRequested = false; in.recording = true;
    f = controller.update(in, 540);
    expect(f, Mode::Listening); assert(f.ageMs == 30);
    in.recording = false;
    expect(controller.update(in, 1000), Mode::Thinking);
    in.speaking = true;
    expect(controller.update(in, 1200), Mode::Speaking);
    in.wanted = false; in.error = true; // OFF precedes error and stale playback.
    f = controller.update(in, 1500);
    expect(f, Mode::Ending); assert(f.ageMs == 0 && f.weight == 1);
    f = controller.update(in, 1900);
    expect(f, Mode::Ending); assert(f.weight == 1 && f.ageMs == 400);
    f = controller.update(in, 2050);
    expect(f, Mode::Ending); assert(near(f.weight, .5f) && f.ageMs == 550);
    expect(controller.update(in, 2199), Mode::Ending);
    f = controller.update(in, 2200);
    expect(f, Mode::Idle); assert(f.weight == 0);
    expect(controller.update(in, 9000), Mode::Idle); // No repeated farewell.
}

static void reenterAndWrap() {
    Controller controller;
    Input in; in.wanted = in.active = in.recording = true;
    controller.update(in, UINT32_MAX - 200);
    auto f = controller.update(in, UINT32_MAX - 50);
    expect(f, Mode::Listening); assert(f.ageMs == 150 && near(f.weight, .5f));
    f = controller.update(in, 99);
    assert(f.ageMs == 300 && f.weight == 1);
    in.wanted = false;
    expect(controller.update(in, 100), Mode::Ending);
    f = controller.update(in, 650);
    assert(near(f.weight, .5f));
    in.wanted = true; in.active = false;
    f = controller.update(in, 650);
    expect(f, Mode::Connecting); assert(f.ageMs == 0 && near(f.weight, .5f));
    f = controller.update(in, 950);
    assert(f.weight == 1);
    in.active = true; in.recording = false;
    expect(controller.update(in, 960), Mode::Thinking);

    Controller ending;
    in.recording = true;
    ending.update(in, UINT32_MAX - 900);
    ending.update(in, UINT32_MAX - 600);
    in.wanted = false;
    ending.update(in, UINT32_MAX - 200);
    f = ending.update(in, 498);
    expect(f, Mode::Ending); assert(f.ageMs == 699);
    expect(ending.update(in, 499), Mode::Idle);

    // Sparse frames have the same time-based fade; no loops or waits required.
    Controller sparse;
    in.wanted = in.active = true;
    expect(sparse.update(in, 10), Mode::Listening);
    f = sparse.update(in, 100000);
    assert(f.weight == 1);
    in.wanted = false;
    sparse.update(in, 100001);
    f = sparse.update(in, 200000);
    expect(f, Mode::Idle); assert(f.weight == 0);
}

int main() {
    bootAndPriority(); transitions(); reenterAndWrap();
    assert(!strcmp(name(Mode::Thinking), "thinking"));
    assert(!strcmp(name(Mode::Unavailable), "unavailable"));
    std::puts("PASS: activity boot/OFF priority, 128 input combinations, strict Speaking, ending/re-entry/fades, millis wrap");
}
