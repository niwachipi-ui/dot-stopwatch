#include <Arduino.h>
#include <M5Unified.h>
#include <algorithm>
#include <cmath>
#include "bag_motion.h"
#include "voice_test.h"
#include "network_bridge.h"
#include "button_probe.h"
#include "button_wave.h"
#include "call_pose.h"
#include "telephone_prop.h"

// Animation plus physical push-to-talk and bounded USB audio transport.
// Your dot is not connected until a separate, explicitly configured host relay.
namespace {
M5Canvas body(&M5.Display), actor(&M5.Display), frame(&M5.Display);
M5Canvas softBodies[4];
bool softBodiesReady = false;
uint32_t lastFrame = 0, reactionAt = 0, blinkAt = 0, nextBlink = 2400;
bool reacting = false, blinking = false;
uint32_t framesPresented = 0, teSynced = 0, teTimeouts = 0;
float gazeX = 0, gazeY = 0;
BagMotion bagMotion;
DotActivity::Controller activityController;
DotActivity::Frame activityFrame{DotActivity::Mode::Idle, 0, 0};
uint32_t activityStartedAt = 0;
uint32_t lastMotionSample = 0, lastMotionUpdate = 0;
bool motionTestPassed = false;
enum class Mood : uint8_t { Look, Bounce, Dance, Spin, Doze, Stretch, Wander, Read, Eat, Laugh, Count };
const char* moodNames[] = {"look", "bounce", "dance", "spin", "doze", "stretch", "wander", "read", "eat", "laugh"};
constexpr uint8_t moodCount = static_cast<uint8_t>(Mood::Count);
Mood mood = Mood::Look;
uint32_t moodAt = 0, moodDuration = 6200, nextMoodAt = 8500, moodChanges = 0;
uint32_t touchAt = 0;
bool touched = false;
// First outing introduces the new actions; subsequent cycles are shuffled.
uint8_t moodBag[] = {7, 8, 9, 5, 0, 1, 2, 3, 4, 6};
uint8_t bagPosition = 0;

void updateMood(uint32_t now) {
    if (static_cast<int32_t>(now - nextMoodAt) < 0) return;
    // Shuffle a bag so every behaviour appears, with no immediate repeat.
    if (bagPosition == moodCount) {
        for (int i = moodCount - 1; i > 0; --i) {
            const int j = esp_random() % (i + 1);
            std::swap(moodBag[i], moodBag[j]);
        }
        if (moodBag[0] == static_cast<uint8_t>(mood)) std::swap(moodBag[0], moodBag[1]);
        bagPosition = 0;
    }
    mood = static_cast<Mood>(moodBag[bagPosition++]);
    moodAt = now;
    moodDuration = (mood == Mood::Doze || mood == Mood::Read || mood == Mood::Eat)
        ? 8500 : 4800 + esp_random() % 2200;
    nextMoodAt = now + moodDuration + 1200 + esp_random() % 1800;
    ++moodChanges;
}
bool networkResetting = false;
uint32_t observedNetworkGeneration = 0;
void serviceNetwork() {
    const uint32_t epoch = NetworkBridge::generation.load();
    if (epoch == observedNetworkGeneration) return;
    // The main task owns the call gate, RX mutex and protocol parser. The TLS
    // task never invokes these functions or touches microphone/speaker state.
    networkResetting = true;
    VoiceTest::phoneOff();
    NetworkBridge::port.resetParser();
    observedNetworkGeneration = epoch;
    NetworkBridge::accepted = epoch;
    networkResetting = false;
}
void physicalPhoneOff() {
    if (!networkResetting && NetworkBridge::configured.load()) {
        // Preserve an idle authenticated session for the next physical ON.
        // Partial byte streams cannot be silently purged and reused.
        portENTER_CRITICAL(&NetworkBridge::mux);
        const bool queued = NetworkBridge::tx.used != 0;
        portEXIT_CRITICAL(&NetworkBridge::mux);
        if (VoiceTest::state == VoiceTest::Sending || NetworkBridge::port.syncRequired || queued)
            NetworkBridge::requestClose();
    }
}
bool networkOwnsVoice() { return NetworkBridge::configured.load(); }
bool configureNetwork(char* line) {
    if (!strcmp(line, "net_info")) { NetworkBridge::info(); return true; }
    if (strncmp(line, "net_", 4)) return false;
    if (VoiceTest::call.wanted || VoiceTest::call.enabled.load() || !VoiceTest::micGuard.load()
        || !VoiceTest::guardLatch.healthy() || VoiceTest::guardLatch.pending()
        || VoiceTest::state != VoiceTest::Idle || M5.Mic.isRunning()) {
        Serial.println("NET_ERROR active"); return true;
    }
    if (!strcmp(line, "net_clear")) {
        Serial.println(NetworkBridge::clear() ? "NET_CLEARED" : "NET_ERROR storage"); return true;
    }
    if (!strncmp(line, "net_pair\t", 9)) {
        char* token = strchr(line + 9, '\t');
        if (!token) { Serial.println("NET_ERROR syntax"); return true; }
        *token++ = 0;
        if (!NetworkBridge::validHost(line + 9) || !NetworkBridge::validToken(token)) {
            Serial.println("NET_ERROR syntax"); return true;
        }
        Serial.println(NetworkBridge::configure(line + 9, token) ? "NET_PAIRED" : "NET_ERROR storage");
        return true;
    }
    Serial.println("NET_ERROR command"); return true;
}

void handlePort(ProtocolPort& port) {
    if (VoiceTest::pumpPort(port)) return;
    char* command = port.commandLine;
    auto& commandSize = port.commandSize;
    auto& commandOverflow = port.commandOverflow;
    size_t budget = 4096;
    while (budget-- && port.available()) {
        const char c = port.read();
        if (c == '\n') {
            command[commandSize] = 0;
            if (commandOverflow) port.println("COMMAND_ERROR length");
            else if (!port.network() && configureNetwork(command)) {}
            else if (!port.network() && ButtonWave::command(command)) {}
            else if (!port.network() && ButtonProbe::command(command)) {}
            else if (VoiceTest::command(command, port)) {}
            else if (port.network()) { port.println("AUDIO_ERROR command"); }
            else if (strcmp(command, "status") == 0) {
                Serial.printf("DOT_READY version=wifi-voice-33 uptime=%lu psram=%u voice=%s brightness=90 rotation=%u frames=%lu te_sync=%lu te_timeout=%lu mood=%s changes=%lu imu=%u samples=%u motion=%d energy=%.3f bounces=%u collisions=%u selftest=%u activity=%s\n",
                    static_cast<unsigned long>(millis()), ESP.getPsramSize(),
                    VoiceTest::call.name(),
                    static_cast<unsigned>(M5.Display.getRotation()),
                    static_cast<unsigned long>(framesPresented),
                    static_cast<unsigned long>(teSynced),
                    static_cast<unsigned long>(teTimeouts), moodNames[static_cast<uint8_t>(mood)],
                    static_cast<unsigned long>(moodChanges), M5.Imu.isEnabled(), bagMotion.samples,
                    bagMotion.mode, bagMotion.energy, bagMotion.steps, bagMotion.collisions, motionTestPassed,
                    DotActivity::name(activityFrame.mode));
                VoiceTest::status();
            } else if (strncmp(command, "wifi\t", 5) == 0) {
                if (VoiceTest::call.wanted || !VoiceTest::micGuard.load() || VoiceTest::guardLatch.pending()) {
                    Serial.println("WIFI_BUSY");
                    memset(command, 0, sizeof(port.commandLine)); commandSize = 0; continue;
                }
                NetworkBridge::requestClose();
                char* password = strchr(command + 5, '\t');
                if (password && password - command - 5 <= 32 && strlen(password + 1) <= 63) {
                    *password++ = 0;
                    WiFi.begin(command + 5, password);
                    Serial.println("WIFI_CONNECTING");
                } else Serial.println("WIFI_INVALID");
            } else if (strcmp(command, "capture") == 0) {
                Serial.print("P6\n466 466\n255\n");
                uint8_t row[466 * 3];
                for (int y = 0; y < 466; ++y) {
                    M5.Touch.update(millis());
                    VoiceTest::updatePhoneControls();
                    frame.readRectRGB(0, y, 466, 1, row);
                    Serial.write(row, sizeof(row));
                }
            }
            memset(command, 0, sizeof(port.commandLine));
            commandSize = 0;
            commandOverflow = false;
            // The just-parsed command may have switched to binary/quarantine.
            if (VoiceTest::pumpPort(port)) return;
        } else if (c != '\r') {
            // Wi-Fi names/passwords may contain UTF-8. The audio command
            // parsers separately enforce strict ASCII numeric fields.
            const uint8_t byte = static_cast<uint8_t>(c);
            if (commandSize < sizeof(port.commandLine) - 1 && ((byte >= 32 && byte != 127) || c == '\t')) command[commandSize++] = c;
            else commandOverflow = true;
        }
    }
}

uint8_t channel(float value) {
    return static_cast<uint8_t>(std::max(0.0f, std::min(255.0f, value)));
}

void makeBody(M5Canvas& target, float swell = 0) {
    target.fillScreen(TFT_BLACK);
    for (int y = 0; y < 280; ++y) {
        for (int x = 0; x < 280; ++x) {
            const float dx = x - 140.0f, dy = y - 140.0f;
            const float theta = atan2f(dy, dx);
            const float rightEdge = (theta - .45f) / .3f;
            const float leftEdge = (theta - (PI - .45f)) / .3f;
            const float localSwell = swell * (expf(-rightEdge * rightEdge) + expf(-leftEdge * leftEdge));
            const float radius = localSwell + 105.0f + 10.0f * cosf(6.0f * theta + 0.8f)
                + 3.0f * sinf(3.0f * theta);
            const float distance = sqrtf(dx * dx + dy * dy);
            if (distance > radius) continue;
            // Stable fine grain and diffuse shading give the flower a soft surface.
            const uint32_t hash = (uint32_t(x) * 73856093u) ^ (uint32_t(y) * 19349663u);
            const float grain = (int(hash % 23u) - 11) * 0.55f;
            const float edge = std::min(1.0f, (radius - distance) / 2.0f);
            const float light = 1.0f - 0.14f * distance / radius - 0.10f * dy / radius;
            target.drawPixel(x, y, target.color565(channel((244 * light + grain) * edge),
                channel((50 * light + grain + 8) * edge),
                channel((155 * light + grain) * edge)));
        }
    }
}

void react(uint32_t now) {
    reacting = true;
    reactionAt = now;
}

void draw(uint32_t now) {
    updateMood(now);
    const auto previousActivity = activityFrame.mode;
    activityFrame = activityController.update(VoiceTest::activityInput(), now);
    if ((previousActivity == DotActivity::Mode::Idle || previousActivity == DotActivity::Mode::Ending)
        && activityFrame.mode != DotActivity::Mode::Idle && activityFrame.mode != DotActivity::Mode::Ending)
        activityStartedAt = now;
    const auto callPose = DotActivity::pose(activityFrame);
    const float attention = activityFrame.weight;
    const bool idleProps = activityFrame.mode == DotActivity::Mode::Idle;
    const float t = now * 0.001f;
    const float p = std::min(1.0f, float(now - moodAt) / moodDuration);
    const float envelope = sinf(PI * p) * sinf(PI * p);
    const float phase = p * 2 * PI;
    float x = 5 * sinf(t * .7f), y = 8 * sinf(t * 1.7f);
    float angle = 4 * sinf(t * 1.1f);
    float stretch = 1 + .018f * sinf(t * 2), eyeOpen = 1, eyeX = 0, eyeY = 0;
    bool smile = false;
    switch (mood) {
        case Mood::Look:
            eyeX = 11 * sinf(phase * 1.5f) * envelope;
            eyeY = -5 * sinf(phase) * envelope;
            angle += 9 * sinf(phase * 1.5f) * envelope;
            break;
        case Mood::Bounce:
            y -= 48 * fabsf(sinf(phase * 2)) * envelope;
            stretch += .08f * cosf(phase * 4) * envelope;
            smile = envelope > .55f;
            break;
        case Mood::Dance:
            x += 26 * sinf(phase * 2) * envelope;
            angle += 18 * sinf(phase * 3) * envelope;
            y -= 13 * fabsf(sinf(phase * 3)) * envelope;
            smile = envelope > .3f;
            break;
        case Mood::Spin:
            angle += 360 * p * p * (3 - 2 * p);
            y -= 18 * envelope;
            break;
        case Mood::Doze:
            eyeOpen = 1 - .93f * envelope;
            y += 13 * envelope;
            angle += 13 * envelope;
            stretch += .045f * sinf(phase * 2) * envelope;
            break;
        case Mood::Stretch:
            stretch -= .16f * sinf(phase) * envelope;
            eyeY -= 6 * envelope;
            break;
        case Mood::Wander:
            x += 34 * sinf(phase) * envelope;
            y += 15 * sinf(phase * 2) * envelope;
            eyeX = 9 * cosf(phase) * envelope;
            angle += 12 * sinf(phase) * envelope;
            break;
        case Mood::Read:
            eyeX = 8 * sinf(phase * 3) * envelope;
            eyeY = 9 * envelope;
            angle *= .35f;
            y *= .3f;
            break;
        case Mood::Eat:
            eyeY = 6 * envelope;
            stretch += .045f * sinf(phase * 6) * envelope;
            angle += 5 * sinf(phase * 3) * envelope;
            break;
        case Mood::Laugh:
            smile = envelope > .2f;
            y -= 12 * fabsf(sinf(phase * 5)) * envelope;
            stretch += .08f * sinf(phase * 5) * envelope;
            angle += 9 * sinf(phase * 3) * envelope;
            break;
        default: break;
    }
    if (idleProps && bagMotion.blend > .1f) {
        eyeOpen = 1;
        eyeX = BagMotion::limit(bagMotion.vx * .02f, -8, 8);
        eyeY = BagMotion::limit(bagMotion.vy * .01f, -4, 4);
        smile = false;
    }
    if (!blinking && static_cast<int32_t>(now - nextBlink) >= 0) {
        blinking = true;
        blinkAt = now;
    }
    float openness = 1.0f;
    if (blinking) {
        const uint32_t elapsed = now - blinkAt;
        if (elapsed >= 180) {
            blinking = false;
            nextBlink = now + 2600 + (esp_random() % 2600);
        } else {
            openness = fabsf(static_cast<float>(elapsed) - 90.0f) / 90.0f;
        }
    }
    float hop = 0;
    if (reacting) {
        const float elapsed = (now - reactionAt) * 0.001f;
        if (elapsed >= 1.2f) reacting = false;
        else hop = 24.0f * fabsf(sinf(elapsed * 9.0f)) * expf(-elapsed * 2.2f);
    }
    gazeX *= 0.96f;
    gazeY *= 0.96f;
    if (idleProps && touched && now - touchAt < 1600) {
        eyeX = gazeX;
        eyeY = gazeY;
        eyeOpen = 1;
        smile = false;
    }
    eyeX = eyeX * (1-attention) + callPose.eyeX * attention;
    eyeY = eyeY * (1-attention) + callPose.eyeY * attention;
    eyeOpen = eyeOpen * (1-attention) + callPose.eyeOpen * attention;
    if (!idleProps) smile = callPose.smile;
    // At most four pixels of continuous silhouette deformation. No added limbs.
    int softLevel = 0;
    if (idleProps && (mood == Mood::Read || mood == Mood::Eat || mood == Mood::Laugh))
        softLevel = int(lroundf(4 * envelope * (.5f + .5f * sinf(phase * 2))));
    if (!idleProps) softLevel = std::max(0, std::min(4, int(lroundf(callPose.swell * attention))));
    if (softBodiesReady && softLevel > 0) softBodies[softLevel - 1].pushSprite(&actor, 0, 0);
    else body.pushSprite(&actor, 0, 0);
    const float walking = bagMotion.blend * (1 - bagMotion.ballBlend) * (1-attention);
    // Bring both eyes around the side; the far eye becomes smaller.
    const float profile = walking * bagMotion.facing;
    eyeX = eyeX * (1 - walking) + 38 * profile;
    const int eyesY = 114 + int(eyeY);
    const int eyeHeight = std::max(2, int(14 * openness * eyeOpen));
    for (int baseX : {119, 160}) {
        const int eyesX = 140 + int((baseX - 140) * (1 - .45f * fabsf(profile)) + eyeX);
        const bool farEye = (profile > 0 && baseX < 140) || (profile < 0 && baseX > 140);
        const int eyeWidth = farEye ? 9 - int(4 * fabsf(profile)) : 9;
        if (smile && openness > .7f) {
            actor.drawWideLine(eyesX - 8, eyesY + 3, eyesX, eyesY - 4, 4, 0x18C3);
            actor.drawWideLine(eyesX, eyesY - 4, eyesX + 8, eyesY + 3, 4, 0x18C3);
        } else {
            actor.fillEllipse(eyesX, eyesY, eyeWidth, eyeHeight, 0x18C3);
            if (eyeHeight > 8) actor.fillEllipse(eyesX - 2, eyesY - 5, 2, 3, 0x3186);
        }
    }

    // No separate hands or fingers. Books rest against the continuous body.
    const float reveal = std::min(1.0f, std::min(p, 1 - p) * 8);
    if (idleProps && mood == Mood::Laugh && envelope > .2f && bagMotion.blend < .1f) {
        actor.fillEllipse(140, 148, 15, 9 + int(5 * fabsf(sinf(phase * 5))), 0x18C3);
        actor.fillEllipse(140, 155, 9, 4, 0xFBB6);
        actor.fillEllipse(100, 139, 10, 5, 0xFB56);
        actor.fillEllipse(180, 139, 10, 5, 0xFB56);
    }
    if (idleProps && mood == Mood::Read && reveal > 0 && bagMotion.blend < .1f) {
        const int by = 166 + int((1 - reveal) * 96);
        actor.fillRoundRect(76, by, 128, 62, 6, 0x3217);
        actor.fillTriangle(82, by + 6, 138, by + 15, 82, by + 51, 0xFF9C);
        actor.fillTriangle(138, by + 15, 138, by + 57, 82, by + 51, 0xFF9C);
        actor.fillTriangle(142, by + 15, 198, by + 6, 198, by + 51, 0xFFF0);
        actor.fillTriangle(142, by + 15, 198, by + 51, 142, by + 57, 0xFFF0);
        for (int line = 0; line < 3; ++line) {
            actor.drawLine(90, by + 20 + line * 10, 129, by + 26 + line * 10, 0xB596);
            actor.drawLine(151, by + 26 + line * 10, 189, by + 20 + line * 10, 0xB596);
        }
        const int pageX = 140 + int(52 * cosf(phase * 2));
        actor.fillTriangle(140, by + 15, pageX, by + 3, 140, by + 57, 0xFFFF);
        // Restore a tiny curved part of the original body over the book edge.
        // Same texture, continuous with the body: no separate hand or fingers.
        M5Canvas& surface = softBodiesReady && softLevel > 0 ? softBodies[softLevel - 1] : body;
        for (int dy = -11; dy <= 11; ++dy) {
            const int py = by + 25 + dy;
            if (py < 0 || py >= 280) continue;
            const int overlap = int(6 * sqrtf(std::max(0.0f, 1 - dy * dy / 121.0f)));
            for (int px = 68; px <= 76 + overlap; ++px) {
                const auto color = surface.readPixel(px, py);
                if (color != TFT_BLACK) actor.drawPixel(px, py, color);
            }
            for (int px = 203 - overlap; px <= 212; ++px) {
                const auto color = surface.readPixel(px, py);
                if (color != TFT_BLACK) actor.drawPixel(px, py, color);
            }
        }
    }
    if (idleProps && mood == Mood::Eat && reveal > 0 && bagMotion.blend < .1f) {
        const int by = 197 + int((1 - reveal) * 76);
        actor.fillEllipse(140, by + 8, 41, 26, 0x5D9F);
        actor.fillEllipse(140, by, 44, 13, 0xFFFF);
        for (int i = 0; i < 7; ++i)
            actor.fillEllipse(111 + i * 9, by - 5 - (i % 3) * 3, 8, 6, 0xFFDE);
        actor.drawWideLine(98, by + 1, 182, by + 1, 3, 0x34DB);
        M5Canvas& surface = softBodiesReady && softLevel > 0 ? softBodies[softLevel - 1] : body;
        for (int dy = -9; dy <= 9; ++dy) {
            const int py = by + 6 + dy;
            if (py < 0 || py >= 280) continue;
            const int overlap = int(5 * sqrtf(std::max(0.0f, 1 - dy * dy / 81.0f)));
            for (int px = 89; px <= 99 + overlap; ++px) {
                const auto color = surface.readPixel(px, py);
                if (color != TFT_BLACK) actor.drawPixel(px, py, color);
            }
            for (int px = 181 - overlap; px <= 190; ++px) {
                const auto color = surface.readPixel(px, py);
                if (color != TFT_BLACK) actor.drawPixel(px, py, color);
            }
        }
        if (envelope > .1f) {
            const float chew = .5f + .5f * sinf(phase * 6);
            actor.fillEllipse(140, 147, 4 + int(2 * chew), 2 + int(2 * chew), 0x18C3);
        }
    }

    if (!idleProps) {
        if (activityFrame.mode == DotActivity::Mode::Speaking && attention > .2f) {
            const int height = 2 + int(8 * callPose.mouth * attention);
            actor.fillEllipse(140, 148, 6 + int(3 * callPose.mouth), height, 0x18C3);
            if (height > 6) actor.fillEllipse(140, 148 + height - 3, 4, 2, 0xFBB6);
        }
        // Let the actor settle in the clear centre before bringing in the
        // phone; during farewell, put it away before resuming an idle pose.
        float phoneReveal = attention >= .999f
            ? DotActivity::smooth((float(now - activityStartedAt) - 300) / 180) : 0;
        if (activityFrame.mode == DotActivity::Mode::Ending)
            phoneReveal *= 1 - DotActivity::smooth((float(activityFrame.ageMs) - 200) / 200);
        DotTelephone::drawTelephone(actor, phoneReveal, callPose.receiver, t);
        // A shallow fold of the very same textured body covers the prop edge.
        // It stays connected to the silhouette: no separately drawn hands.
        if (phoneReveal > .95f && callPose.receiver > .95f) {
            M5Canvas& surface = softBodiesReady && softLevel > 0 ? softBodies[softLevel - 1] : body;
            for (int dy = -10; dy <= 10; ++dy) {
                const int fold = int(4 * sqrtf(std::max(0.0f, 1 - dy * dy / 100.0f)));
                for (int px = 218; px <= 229 + fold; ++px) {
                    const auto color = surface.readPixel(px, 166 + dy);
                    if (color != TFT_BLACK) actor.drawPixel(px, 166 + dy, color);
                }
            }
            for (int dy = -8; dy <= 8; ++dy) {
                const int fold = int(4 * sqrtf(std::max(0.0f, 1 - dy * dy / 64.0f)));
                for (int px = 86; px <= 89 + fold; ++px) {
                    const auto color = surface.readPixel(px, 220 + dy);
                    if (color != TFT_BLACK) actor.drawPixel(px, 220 + dy, color);
                }
                for (int px = 190 - fold; px <= 195; ++px) {
                    const auto color = surface.readPixel(px, 220 + dy);
                    if (color != TFT_BLACK) actor.drawPixel(px, 220 + dy, color);
                }
            }
        }
    }
    frame.fillScreen(TFT_BLACK);
    frame.setTextDatum(middle_center);
    frame.setFont(&fonts::Font2);
    frame.setTextColor(0xBDF7);
    frame.setTextSize(1.2f);
    frame.fillEllipse(233, 357, 65 - int(hop / 4), 8, 0x1842);
    const float moving = bagMotion.blend * (1-attention);
    const float size = (1 - .12f * walking - .32f * bagMotion.ballBlend) * (1-attention) + .94f * attention;
    x = x * (1 - moving) + bagMotion.x * moving;
    y = (y - hop) * (1 - moving) + (6 + bagMotion.y) * moving;
    angle = angle * (1 - moving) + BagMotion::limit(bagMotion.vx * .065f, -28, 28) * moving;
    stretch = stretch * (1 - moving) + (1 + .035f * sinf(t * 12)) * moving;
    angle = angle * (1 - walking) + (6 * bagMotion.facing + 3 * sinf(bagMotion.gait)) * walking;
    stretch = stretch * (1 - walking) + (.91f + .035f * cosf(bagMotion.gait * 2)) * walking;
    x = x * (1-attention) + callPose.x * attention;
    y = y * (1-attention) + callPose.y * attention;
    if (!idleProps) angle = remainderf(angle, 360);
    angle = angle * (1-attention) + callPose.angle * attention;
    stretch = stretch * (1-attention) + callPose.stretch * attention;
    actor.pushRotateZoom(&frame, 233 + x, 227 + y, angle,
        stretch * size, size / stretch, TFT_BLACK);
    if (activityFrame.mode == DotActivity::Mode::Thinking && attention >= .999f) {
        // Add one bubble every 800 ms, then clear all for 800 ms. ageMs
        // restarts on entry to Thinking, so every reply begins with the dot.
        const uint32_t phaseMs = activityFrame.ageMs % 4000;
        const int visible = phaseMs < 3200 ? 1 + int(phaseMs / 800) : 0;
        const uint16_t outline = frame.color565(220, 224, 232);
        const int bubbles[][3] = {{331, 152, 2}, {343, 145, 4}, {359, 136, 7}, {380, 124, 10}};
        for (int i = 0; i < visible; ++i) {
            const auto& bubble = bubbles[i];
            frame.fillCircle(bubble[0], bubble[1], bubble[2], outline);
            if (i > 0) frame.fillCircle(bubble[0], bubble[1], bubble[2] - 2, TFT_BLACK);
        }
    }
    if (idleProps && mood == Mood::Doze && bagMotion.blend < .1f && envelope > .55f && !(touched && now - touchAt < 1600)) {
        frame.setTextColor(0xDCD9);
        frame.setTextSize(1);
        frame.drawString("z", 330, 143 - int(6 * sinf(phase)));
        frame.setTextSize(1.4f);
        frame.drawString("z", 348, 119 - int(6 * sinf(phase)));
    }
    VoiceTest::overlay(frame, activityFrame.mode);
    // StopWatch uses an automatically flushed AMOLED framebuffer by default.
    // Copy the completed frame without intermediate flushes, then present once
    // at the panel's TE rising edge. Never change brightness during animation.
    // FrameBufferBase::waitDMA is a no-op; wait on its actual QSPI bus.
    M5.Display.getPanel()->getBus()->wait();
    frame.pushSprite(0, 0);
    const uint32_t syncStarted = micros();
    while (digitalRead(38) && micros() - syncStarted < 22000) delayMicroseconds(10);
    while (!digitalRead(38) && micros() - syncStarted < 22000) delayMicroseconds(10);
    if (micros() - syncStarted < 22000) ++teSynced;
    else ++teTimeouts;
    M5.Display.display();
    M5.Display.getPanel()->getBus()->wait();
    ++framesPresented;
}
}

void setup() {
    Serial.setRxBufferSize(16384);
    Serial.setTxTimeoutMs(50);
    Serial.begin(115200);
    auto cfg = M5.config();
    cfg.clear_display = true;
    M5.begin(cfg);
    // Strap/USB edge at the top when hanging from the user's bag.
    // M5.Touch uses Display.convertRawXY, so touch follows this rotation.
    M5.Display.setRotation(2);
    M5.Display.setBrightness(90);
    M5.Display.setAutoDisplay(false);
    for (M5Canvas* canvas : {&body, &actor, &frame}) {
        canvas->setColorDepth(16);
        canvas->setPsram(true);
    }
    if (!body.createSprite(280, 280) || !actor.createSprite(280, 280)
        || !frame.createSprite(466, 466)) {
        M5.Display.setTextColor(TFT_WHITE, TFT_BLACK);
        M5.Display.println("Display buffer allocation failed");
        while (true) delay(1000);
    }
    actor.setPivot(140, 140);
    motionTestPassed = bagMotionSelfTest();
    makeBody(body);
    softBodiesReady = true;
    for (int i = 0; i < 4; ++i) {
        softBodies[i].setColorDepth(16);
        softBodies[i].setPsram(true);
        if (!softBodies[i].createSprite(280, 280)) { softBodiesReady = false; break; }
        makeBody(softBodies[i], i + 1);
    }
    VoiceTest::begin();
    NetworkBridge::begin();
    VoiceTest::transportService = serviceNetwork;
    VoiceTest::phoneOffHook = physicalPhoneOff;
    VoiceTest::networkOwner = networkOwnsVoice;
    serviceNetwork();
    moodAt = millis();
    nextMoodAt = moodAt + 8500;
    draw(millis());
}

void loop() {
    ButtonWave::update();
    ButtonProbe::update();
    if (!ButtonProbe::skipUpdate()) M5.update();
    bool phoneTouch = VoiceTest::updatePhoneControls();
    handlePort(usbProtocol);
    serviceNetwork();
    handlePort(NetworkBridge::port);
    phoneTouch = VoiceTest::updatePhoneControls() || phoneTouch;
    const uint32_t now = millis();
    if (now - lastMotionSample >= 20) {
        const float dt = lastMotionSample ? (now - lastMotionSample) * .001f : .02f;
        lastMotionSample = now;
        float ax, ay, az;
        if (!ButtonProbe::skipImu() && M5.Imu.isEnabled() && M5.Imu.getAccel(&ax, &ay, &az))
            bagMotion.sample(ax, ay, az, dt);
        else bagMotion.energy *= .8f;
    }
    bagMotion.advance(lastMotionUpdate ? (now - lastMotionUpdate) * .001f : .002f);
    lastMotionUpdate = now;
    if (!phoneTouch && M5.Touch.getCount()) {
        const auto touch = M5.Touch.getDetail();
        if (touch.wasPressed()) react(now);
        if (touch.isPressed()) {
            touched = true;
            touchAt = now;
            gazeX = std::max(-8.0f, std::min(8.0f, (touch.x - 233) * 0.07f));
            gazeY = std::max(-5.0f, std::min(5.0f, (touch.y - 233) * 0.04f));
        }
    }
    if (M5.BtnA.wasPressed()) react(now);
    // Yellow hold is volume-down in v21; no local replay gesture.
    // A complete frame remains displayed while USB PCM arrives. Skipping
    // expensive QSPI redraws keeps the receive queue drained without flicker;
    // button, serial, touch and IMU updates continue normally.
    if (!VoiceTest::receiving && !ButtonProbe::skipDraw() && now - lastFrame >= 33) {
        lastFrame = now;
        draw(now);
    }
    delay(2);
}
