#include "face_controller.h"

#include <Arduino.h>
#include <Avatar.h>
#include <esp_system.h>

#include <cstring>

using namespace m5avatar;

static Avatar* s_avatar = nullptr;

static constexpr unsigned long MOUTH_OPEN_MIN_MS = 80;
static constexpr unsigned long MOUTH_OPEN_MAX_MS = 200;
static constexpr unsigned long MOUTH_CLOSE_MIN_MS = 30;
static constexpr unsigned long MOUTH_CLOSE_MAX_MS = 100;
static constexpr float MOUTH_OPEN_RATIO_MIN = 0.6f;
static constexpr float MOUTH_OPEN_RATIO_MAX = 1.0f;
static constexpr uint32_t HAPPY_CHANCE_PERCENT = 15;
static constexpr unsigned long HAPPY_DURATION_MIN_MS = 400;
static constexpr unsigned long HAPPY_DURATION_MAX_MS = 800;

static bool s_anim_active = false;
static bool s_mouth_open = false;
static unsigned long s_mouth_timer_ms = 0;
static unsigned long s_mouth_duration_ms = 0;
static bool s_happy_active = false;
static unsigned long s_happy_timer_ms = 0;
static unsigned long s_happy_duration_ms = 0;

static uint32_t rand_range(uint32_t min_val, uint32_t max_val) {
    return min_val + (esp_random() % (max_val - min_val + 1));
}

void face_init() {
    if (s_avatar != nullptr) {
        Serial.println("[Face] Already initialized");
        return;
    }

    s_avatar = new Avatar();
    s_avatar->init();
    Serial.println("[Face] Avatar initialized");
}

void face_set_expression(const char* expr) {
    if (s_avatar == nullptr) {
        Serial.println("[Face] Not initialized");
        return;
    }

    Expression expression = Expression::Neutral;

    if (strcmp(expr, "happy") == 0) {
        expression = Expression::Happy;
    } else if (strcmp(expr, "sad") == 0) {
        expression = Expression::Sad;
    } else if (strcmp(expr, "angry") == 0) {
        expression = Expression::Angry;
    } else if (strcmp(expr, "doubt") == 0) {
        expression = Expression::Doubt;
    } else if (strcmp(expr, "sleepy") == 0) {
        expression = Expression::Sleepy;
    } else if (strcmp(expr, "neutral") == 0) {
        expression = Expression::Neutral;
    } else {
        Serial.printf("[Face] Unknown expression \"%s\", falling back to neutral\n", expr);
    }

    s_avatar->setExpression(expression);
    Serial.printf("[Face] Expression set to \"%s\"\n", expr);
}

void face_suspend() {
    if (s_avatar) {
        s_avatar->suspend();
        Serial.println("[Face] Suspended");
    }
}

void face_resume() {
    if (s_avatar) {
        s_avatar->resume();
        Serial.println("[Face] Resumed");
    }
}

void face_speak_anim_start() {
    if (s_avatar == nullptr) return;

    s_anim_active = true;
    s_mouth_open = true;
    float ratio = MOUTH_OPEN_RATIO_MIN + (float)(esp_random() % 1000) / 1000.0f *
                                             (MOUTH_OPEN_RATIO_MAX - MOUTH_OPEN_RATIO_MIN);
    s_avatar->setMouthOpenRatio(ratio);
    s_mouth_timer_ms = millis();
    s_mouth_duration_ms = rand_range(MOUTH_OPEN_MIN_MS, MOUTH_OPEN_MAX_MS);
    s_happy_active = false;
    Serial.println("[Face] Speak anim start");
}

void face_speak_anim_stop() {
    s_anim_active = false;
    s_mouth_open = false;
    if (s_avatar != nullptr) {
        s_avatar->setMouthOpenRatio(0.0f);
        s_avatar->setExpression(Expression::Neutral);
    }
    s_happy_active = false;
    Serial.println("[Face] Speak anim stop");
}

void face_speak_anim_update() {
    if (!s_anim_active || s_avatar == nullptr) return;

    unsigned long now = millis();

    if (s_happy_active && (now - s_happy_timer_ms >= s_happy_duration_ms)) {
        s_avatar->setExpression(Expression::Neutral);
        s_happy_active = false;
    }

    if (now - s_mouth_timer_ms >= s_mouth_duration_ms) {
        if (s_mouth_open) {
            s_mouth_open = false;
            s_avatar->setMouthOpenRatio(0.0f);
            s_mouth_duration_ms = rand_range(MOUTH_CLOSE_MIN_MS, MOUTH_CLOSE_MAX_MS);
            if (!s_happy_active && rand_range(1, 100) <= HAPPY_CHANCE_PERCENT) {
                s_avatar->setExpression(Expression::Happy);
                s_happy_active = true;
                s_happy_timer_ms = now;
                s_happy_duration_ms = rand_range(HAPPY_DURATION_MIN_MS, HAPPY_DURATION_MAX_MS);
            }
        } else {
            s_mouth_open = true;
            float ratio = MOUTH_OPEN_RATIO_MIN + (float)(esp_random() % 1000) / 1000.0f *
                                                     (MOUTH_OPEN_RATIO_MAX - MOUTH_OPEN_RATIO_MIN);
            s_avatar->setMouthOpenRatio(ratio);
            s_mouth_duration_ms = rand_range(MOUTH_OPEN_MIN_MS, MOUTH_OPEN_MAX_MS);
        }
        s_mouth_timer_ms = now;
    }
}
