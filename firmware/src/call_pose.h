#pragma once
#include <math.h>
#include "dot_activity.h"

namespace DotActivity {
// Decorative motion only. No PCM is read and the mouth is not phoneme tracking.
struct Pose {
    float x = 0, y = 0, angle = 0, stretch = 1;
    float eyeX = 0, eyeY = 0, eyeOpen = 1, mouth = 0;
    float receiver = 1, swell = 2;
    bool smile = false;
};
inline float unit(float x) { return fmaxf(0, fminf(1, x)); }
inline float smooth(float x) { x = unit(x); return x*x*(3-2*x); }
inline Pose pose(const Frame& frame) {
    Pose p;
    const float t = frame.ageMs * .001f;
    switch (frame.mode) {
        case Mode::Connecting:
            p.receiver = smooth((t - .48f) / .45f);
            p.y = -9 * sinf(3.14159265f * unit(t / .65f));
            p.angle = -3 + 2 * sinf(t * 2);
            p.eyeX = 3 * sinf(t * 1.4f);
            p.eyeY = -2;
            p.stretch = 1 + .025f * sinf(t * 3);
            break;
        case Mode::Listening: {
            const float nod = powf(fmaxf(0, sinf(t * 1.8f)), 6);
            p.y = 4 * nod;
            p.angle = -5 + 2 * sinf(t * 1.5f);
            p.eyeY = 2 * nod;
            p.stretch = 1 - .012f * nod;
            p.swell = 3;
            break;
        }
        case Mode::Thinking:
            p.x = 1.5f * sinf(t);
            p.y = 2 * sinf(t * 1.6f);
            p.angle = 5 + 2 * sinf(t * 1.1f);
            p.eyeX = 7;
            p.eyeY = -6;
            p.eyeOpen = .88f;
            p.stretch = 1 + .016f * sinf(t * 2.2f);
            break;
        case Mode::Speaking:
            p.x = 2 * sinf(t * 2.3f);
            p.y = -3 * fabsf(sinf(t * 5));
            p.angle = 3 * sinf(t * 2.7f);
            p.stretch = 1 + .026f * sinf(t * 7);
            p.eyeOpen = .96f;
            p.mouth = .15f + .85f * fabsf(sinf(t * 10) * cosf(t * 3.1f));
            p.swell = 2.5f + 1.5f * sinf(t * 4);
            break;
        case Mode::Ending: {
            const float bow = sinf(3.14159265f * unit(t / .7f));
            p.y = 7 * bow;
            p.eyeY = 5 * bow;
            p.eyeOpen = 1 - .25f * bow;
            p.stretch = 1 + .035f * bow;
            p.smile = true;
            p.receiver = 1 - smooth(t / .4f);
            break;
        }
        case Mode::Unavailable:
            p.angle = -8;
            p.eyeY = 4;
            p.eyeOpen = .8f;
            p.receiver = .65f;
            break;
        case Mode::Idle: p.receiver = 0; p.swell = 0; break;
    }
    return p;
}
}
