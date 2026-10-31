#pragma once
#include <stdint.h>
#include <math.h>

// A small rotary telephone only. The caller supplies Dot's continuous body;
// this prop never draws hands, fingers, arms, or clears the actor background.
namespace DotTelephone {
namespace detail {
inline float unit(float value) {
    return value < 0 ? 0 : value > 1 ? 1 : value;
}
inline int pixel(float value) { return int(lroundf(value)); }

template<class Canvas> struct Paint {
    Canvas& canvas;
    float reveal;
    // Rise from the lower abdomen, unfolding vertically. Even during reveal,
    // the complete prop fits the 280px actor; the bottom stays below y=278.
    float scale() const { return .3f + .7f * reveal; }
    float y(float value) const { return 250 + (value - 250) * scale() + 26 * (1 - reveal); }
    void ellipse(float x, float cy, float rx, float ry, uint16_t color) {
        canvas.fillEllipse(pixel(x), pixel(y(cy)), pixel(rx),
                           pixel(ry * scale()) > 0 ? pixel(ry * scale()) : 1, color);
    }
    void rect(float x, float cy, int width, int height, int radius, uint16_t color) {
        canvas.fillRoundRect(pixel(x), pixel(y(cy)), width,
                             pixel(height * scale()), pixel(radius * scale()), color);
    }
    void line(float x1, float y1, float x2, float y2, float width, uint16_t color) {
        canvas.drawWideLine(x1, y(y1), x2, y(y2), width * scale(), color);
    }
    void triangle(float x1, float y1, float x2, float y2, float x3, float y3, uint16_t color) {
        canvas.fillTriangle(pixel(x1), pixel(y(y1)), pixel(x2), pixel(y(y2)),
                            pixel(x3), pixel(y(y3)), color);
    }
};
}

template<class Canvas>
void drawTelephone(Canvas& actor, float reveal, float lift, float t) {
    if (!isfinite(reveal) || !isfinite(lift) || !isfinite(t) || reveal <= 0) return;
    reveal = detail::unit(reveal);
    lift = detail::unit(lift);
    detail::Paint<Canvas> p{actor, reveal};
    constexpr uint16_t outline = 0x7BF0, edge = 0x4208, black = 0x1082;
    constexpr uint16_t face = 0x2104, sheen = 0x632C, ivory = 0xEED6;
    constexpr float pi = 3.14159265358979323846f;

    const float eased = lift * lift * (3 - 2 * lift);
    const float angle = eased * pi * .5f;
    const float cs = cosf(angle), sn = sinf(angle);
    const float cx = 140 + 82 * eased;
    const float cy = 195 - 38 * eased;
    const auto hx = [=](float x, float y) { return cx + x * cs - y * sn; };
    const auto hy = [=](float x, float y) { return cy + x * sn + y * cs; };

    // Coiled cord is behind both the base and receiver, from the receiver's
    // lower/right cap to the base's right flank. It carries no extra limb.
    const float cordX = hx(29, 4), cordY = hy(29, 4);
    float lastX = cordX, lastY = cordY;
    const float sway = .6f * sinf(t * 1.8f) * eased;
    for (int i = 1; i <= 40; ++i) {
        const float u = i / 40.0f, bow = sinf(pi * u);
        const float x = cordX * (1 - u) + 187 * u + (21 + sway) * bow
            + 2.5f * sinf(u * 12 * pi) * bow;
        const float y = cordY * (1 - u) + 227 * u + 10 * bow;
        p.line(lastX, lastY, x, y, 3.5f, sheen);
        p.line(lastX, lastY, x, y, 1.5f, black);
        lastX = x; lastY = y;
    }

    // Slightly sloped, broad black base with a readable grey rim.
    p.rect(88, 211, 104, 40, 10, outline);
    p.triangle(98, 198, 182, 198, 192, 219, outline);
    p.triangle(98, 198, 192, 219, 88, 219, outline);
    p.rect(90, 213, 100, 36, 8, black);
    p.triangle(100, 200, 180, 200, 188, 217, face);
    p.triangle(100, 200, 188, 217, 92, 217, face);
    p.line(99, 202, 181, 202, 1.5f, sheen);
    p.line(97, 244, 181, 244, 1, edge);
    // Cradle pins stay on the base when the handset is lifted.
    p.rect(101, 195, 9, 11, 3, edge);
    p.rect(170, 195, 9, 11, 3, edge);
    p.line(103, 196, 107, 196, 1.5f, outline);
    p.line(172, 196, 176, 196, 1.5f, outline);

    // Ivory rotary dial: ten dark finger holes and a central number plate.
    p.ellipse(140, 225, 21, 19, edge);
    p.ellipse(140, 224, 19, 17, ivory);
    p.ellipse(140, 224, 10, 9, black);
    for (int i = 0; i < 10; ++i) {
        const float a = (-125 + i * 29) * pi / 180;
        p.ellipse(140 + 14.5f * cosf(a), 224 + 12.5f * sinf(a), 2.5f, 2.5f, black);
    }
    p.ellipse(140, 224, 6, 5.5f, ivory);
    p.line(136, 224, 144, 224, 1, sheen);
    p.line(157, 231, 161, 229, 2.5f, outline); // Dial finger stop.

    // Curved handset bridge plus thick end caps; rotate as one prop from
    // horizontal on the cradle to vertical beside the right cheek.
    constexpr float points[5][2] = {{-29, 0}, {-22, -7}, {0, -10}, {22, -7}, {29, 0}};
    for (int pass = 0; pass < 2; ++pass) {
        const float width = pass ? 8 : 11;
        const uint16_t color = pass ? black : outline;
        for (int i = 1; i < 5; ++i)
            p.line(hx(points[i-1][0], points[i-1][1]), hy(points[i-1][0], points[i-1][1]),
                   hx(points[i][0], points[i][1]), hy(points[i][0], points[i][1]), width, color);
        for (int side = -1; side <= 1; side += 2) {
            const float end = side * 30;
            p.line(hx(end, -3), hy(end, -3), hx(end, 5), hy(end, 5), pass ? 15 : 18, color);
        }
    }
    p.line(hx(-18, -9), hy(-18, -9), hx(15, -10), hy(15, -10), 1.5f, sheen);
    for (int side = -1; side <= 1; side += 2) {
        const float end = side * 30;
        p.line(hx(end - 4, 3), hy(end - 4, 3), hx(end + 4, 3), hy(end + 4, 3), 1, edge);
    }
}
}
