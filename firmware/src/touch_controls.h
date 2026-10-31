#pragma once
#include <stdint.h>
#include <stddef.h>
#include <cstdlib>

// Display-rotation-adjusted coordinates. Pure gesture recognition, no audio
// or call side effects. Repeated samples from the same frame are idempotent.
class WatchTouchControls {
public:
    struct Result { int callButton = 0, volumeDelta = 0; bool consumed = false; };
    static int phoneHit(int x, int y) {
        if (y < 374 || y >= 430) return 0;
        if (x >= 115 && x < 225) return 1;
        if (x >= 241 && x < 351) return 2;
        return 0;
    }
    Result update(size_t contacts, bool pressed, int x, int y) {
        Result result;
        if (!contacts) { reset(); return result; }
        if (contacts > 1) { blocked_ = true; active_ = false; }
        if (blocked_) { result.consumed = true; return result; }
        if (!active_) {
            if (!pressed) return result;
            active_ = true; startX_ = x; startY_ = y;
            button_ = phoneHit(x,y); moved_ = swipe_ = false; steps_ = 0;
            volumeArea_ = !button_ && x >= 50 && x < 416 && y >= 96 && y < 368;
        }
        const int dx = x-startX_, dy = y-startY_;
        if (std::abs(dx) > 12 || std::abs(dy) > 12) moved_ = true;
        if (volumeArea_ && std::abs(dy) >= 24 && std::abs(dy)*2 > std::abs(dx)*3) swipe_ = true;
        result.consumed = button_ || swipe_;
        if (swipe_) {
            const int steps = -dy/18; // Up raises volume; about 5% per step.
            result.volumeDelta = (steps-steps_)*13;
            steps_ = steps;
        }
        if (!pressed) {
            if (!moved_ && button_ && phoneHit(x,y) == button_) result.callButton = button_;
            active_ = false;
        }
        return result;
    }
    void reset() { active_ = blocked_ = swipe_ = moved_ = volumeArea_ = false; button_ = steps_ = 0; }
private:
    bool active_ = false, blocked_ = false, swipe_ = false, moved_ = false, volumeArea_ = false;
    int startX_ = 0, startY_ = 0, button_ = 0, steps_ = 0;
};
