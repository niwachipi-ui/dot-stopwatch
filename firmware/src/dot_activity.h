#pragma once
#include <stdint.h>

// Display-only state: no audio commands, hardware access or recording authority.
namespace DotActivity {
enum class Mode { Idle, Connecting, Listening, Thinking, Speaking, Ending, Unavailable };

struct Input {
    bool wanted = false;
    bool active = false;
    bool healthy = true;
    bool error = false;
    bool recording = false;
    bool listenRequested = false;
    bool speaking = false;
};

struct Frame {
    Mode mode;
    float weight;
    uint32_t ageMs;
};

inline const char* name(Mode mode) {
    switch (mode) {
    case Mode::Idle: return "idle";
    case Mode::Connecting: return "connecting";
    case Mode::Listening: return "listening";
    case Mode::Thinking: return "thinking";
    case Mode::Speaking: return "speaking";
    case Mode::Ending: return "ending";
    case Mode::Unavailable: return "unavailable";
    }
    return "unknown";
}

class Controller {
public:
    static constexpr uint32_t fadeMs = 300;
    static constexpr uint32_t endingMs = 700;

    Frame update(const Input& input, uint32_t now) {
        if (!initialized_) {
            initialized_ = true;
            modeAt_ = now;
        }
        const float previousWeight = weightAt(now);
        Mode next;
        if (!input.wanted) {
            // Booting with OFF is simply Idle. Only an actual ON -> OFF edge
            // produces the brief farewell, independent of stale audio flags.
            next = wasWanted_ || (mode_ == Mode::Ending && elapsed(now) < endingMs)
                ? Mode::Ending : Mode::Idle;
        } else if (!input.healthy || input.error) next = Mode::Unavailable;
        else if (!input.active) next = Mode::Connecting;
        else if (input.speaking) next = Mode::Speaking;
        else if (input.recording || input.listenRequested) next = Mode::Listening;
        else next = Mode::Thinking; // Preparing a reply, not a Mac task status.

        if (next != mode_) {
            mode_ = next;
            modeAt_ = now;
            startWeight_ = previousWeight;
        }
        wasWanted_ = input.wanted;
        return Frame{mode_, weightAt(now), elapsed(now)};
    }

private:
    Mode mode_ = Mode::Idle;
    uint32_t modeAt_ = 0;
    float startWeight_ = 0;
    bool initialized_ = false, wasWanted_ = false;

    uint32_t elapsed(uint32_t now) const { return uint32_t(now - modeAt_); }
    float weightAt(uint32_t now) const {
        if (mode_ == Mode::Idle) return 0;
        const uint32_t age = elapsed(now);
        const float progress = age >= fadeMs ? 1.0f : float(age) / float(fadeMs);
        const float fadeIn = startWeight_ + (1.0f - startWeight_) * progress;
        if (mode_ != Mode::Ending) return fadeIn;
        if (age >= endingMs) return 0;
        const uint32_t remaining = endingMs - age;
        return fadeIn * (remaining >= fadeMs ? 1.0f : float(remaining) / float(fadeMs));
    }
};
}
