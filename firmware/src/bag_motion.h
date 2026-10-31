#pragma once
#include <algorithm>
#include <cmath>

// Acceleration in g, time in seconds, positions in pixels.
// This detects movement intensity, not transport type or a medical step count.
struct BagMotion {
    int mode = 0; // quiet, sideways walking, pinball
    int walkDirection = 1;
    float gait = 0, facing = 1;
    unsigned turns = 0;
    float energy = 0, x = 0, y = 0, vx = 0, vy = 0, blend = 0, ballBlend = 0;
    unsigned samples = 0, steps = 0, collisions = 0;
    float gx = 0, gy = 0, gz = 1, lx = 0, ly = 0;
    float quiet = 0, strongHold = 0, cooldown = 0, warmup = 0;
    bool primed = false, peakArmed = true;
    static float limit(float v, float lo, float hi) { return std::max(lo, std::min(hi, v)); }

    void sample(float ax, float ay, float az, float dt) {
        if (!std::isfinite(ax) || !std::isfinite(ay) || !std::isfinite(az)) return;
        dt = limit(dt, .001f, .2f);
        ++samples;
        if (!primed) { gx = ax; gy = ay; gz = az; primed = true; return; }
        const float a = 1 - expf(-dt / .5f);
        gx += (ax - gx) * a; gy += (ay - gy) * a; gz += (az - gz) * a;
        lx = ax - gx; ly = ay - gy;
        const float dynamic = sqrtf(lx * lx + ly * ly + (az - gz) * (az - gz));
        energy += (dynamic - energy) * (1 - expf(-dt / .18f));
        warmup += dt;
        if (warmup < .7f) return;
        if (energy > .65f || dynamic > 1.2f) {
            if (mode != 2) { vx = lx < 0 ? -245 : 245; vy = -280; }
            mode = 2; strongHold = 2.5f;
        } else if (mode == 0 && energy > .10f) mode = 1;
        if (dynamic < .10f) peakArmed = true;
        if (mode == 1 && peakArmed && cooldown <= 0 && dynamic > .18f) {
            ++steps; cooldown = .28f; peakArmed = false;
        }
    }

    void advance(float dt) {
        dt = limit(dt, .001f, .12f);
        strongHold = std::max(0.0f, strongHold - dt);
        cooldown = std::max(0.0f, cooldown - dt);
        quiet = energy < .065f ? quiet + dt : 0;
        if (mode == 2 && strongHold <= 0) mode = 1;
        if (quiet > 2.0f && strongHold <= 0) mode = 0;
        const float smooth = 1 - expf(-dt / .35f);
        blend += ((mode ? 1.0f : 0.0f) - blend) * smooth;
        ballBlend += ((mode == 2 ? 1.0f : 0.0f) - ballBlend) * smooth;
        if (mode == 1) {
            gait = fmodf(gait + dt * (9 + limit(energy * 8, 0, 3)), 6.2831853f);
            if ((walkDirection > 0 && x > 48) || (walkDirection < 0 && x < -48)) {
                walkDirection = -walkDirection;
                ++turns;
            }
        }
        facing += (walkDirection - facing) * (1 - expf(-dt / .18f));
        for (int i = 0; i < 6; ++i) {
            const float h = dt / 6;
            if (mode == 2) {
                vx += limit(-lx * 300, -350, 350) * h;
                vy += (140 + limit(ly * 300, -350, 350)) * h;
                const float speed = hypotf(vx, vy);
                if (speed > 480) { vx *= 480 / speed; vy *= 480 / speed; }
            } else if (mode == 1) {
                // Deliberate screen traversal, not a spring pulling back to center.
                vx += (walkDirection * 52 - vx) * std::min(1.0f, h * 9);
                const float footfall = -9 * fabsf(sinf(gait));
                vy += ((footfall - y) * 70 - vy * 13) * h;
            } else {
                vx += (-x * 12 - vx * 4) * h;
                vy += (-y * 18 - vy * 3.5f) * h;
            }
            x += vx * h; y += vy * h;
            // Bound the entire character, including stretch, inside the circle.
            const float radius = 230 - 170 * (1 - .32f * ballBlend);
            const float distance = hypotf(x, y);
            if (distance > radius) {
                const float nx = x / distance, ny = y / distance;
                x = nx * radius; y = ny * radius;
                const float outward = vx * nx + vy * ny;
                if (outward > 0) {
                    vx -= 1.9f * outward * nx; vy -= 1.9f * outward * ny;
                    ++collisions;
                }
            }
        }
    }
};

// Synthetic inputs exercise transitions and edge containment without moving hardware.
inline bool bagMotionSelfTest() {
    BagMotion m;
    for (int i = 0; i < 150; ++i) { m.sample(0, 0, 1, .02f); m.advance(.02f); }
    if (m.mode != 0) return false;
    for (int i = 0; i < 150; ++i) { m.sample(0, 0, 1 + .32f * sinf(i * .25f), .02f); m.advance(.02f); }
    if (m.mode != 1 || !m.steps || !m.turns || fabsf(m.x) < 10) return false;
    for (int i = 0; i < 300; ++i) {
        m.sample(1.5f * sinf(i * .6f), .8f * cosf(i * .7f), 1, .02f); m.advance(.02f);
        if (!std::isfinite(m.x) || hypotf(m.x, m.y) > 230 - 170 * (1 - .32f * m.ballBlend) + .1f) return false;
    }
    if (m.mode != 2 || !m.collisions) return false;
    for (int i = 0; i < 500; ++i) { m.sample(0, 0, 1, .02f); m.advance(.02f); }
    return m.mode == 0 && m.blend < .01f && hypotf(m.x, m.y) < 1;
}
