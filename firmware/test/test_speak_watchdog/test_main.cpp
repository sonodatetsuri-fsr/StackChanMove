// ---------------------------------------------------------------------------
// speak_watchdog.h の単体テスト。
// ホスト実行: `pio test -e native`
// ---------------------------------------------------------------------------
#include <unity.h>

#include <cstdint>

#include "speak_watchdog.h"

static constexpr uint32_t kTimeout = 5000;

void setUp(void) {}
void tearDown(void) {}

static void test_default_timeout_is_5000(void) {
    TEST_ASSERT_EQUAL_UINT32(5000, SPEAK_RX_TIMEOUT_MS);
}

static void test_not_speaking_never_stops(void) {
    TEST_ASSERT_FALSE(spkwd::should_stop(false, 0, 0, kTimeout));
    TEST_ASSERT_FALSE(spkwd::should_stop(false, 0, 100000, kTimeout));
    TEST_ASSERT_FALSE(spkwd::should_stop(false, 0xFFFFFF00u, 0x00010000u, kTimeout));
}

static void test_within_timeout_keeps_speaking(void) {
    TEST_ASSERT_FALSE(spkwd::should_stop(true, 1000, 1000, kTimeout));
    TEST_ASSERT_FALSE(spkwd::should_stop(true, 1000, 1500, kTimeout));  // 0.5 s 間隔の音声
    TEST_ASSERT_FALSE(spkwd::should_stop(true, 1000, 5999, kTimeout));  // 4999 ms
}

static void test_timeout_boundary_stops(void) {
    TEST_ASSERT_TRUE(spkwd::should_stop(true, 1000, 6000, kTimeout));  // ちょうど 5000 ms
    TEST_ASSERT_TRUE(spkwd::should_stop(true, 1000, 6001, kTimeout));
    TEST_ASSERT_TRUE(spkwd::should_stop(true, 1000, 60000, kTimeout));
    TEST_ASSERT_EQUAL_UINT32(5001, spkwd::elapsed_ms(1000, 6001));
}

// 音声フレームを受けるたびに起点を更新すれば、発話が長くても止めない。
static void test_audio_frames_restart_timer(void) {
    uint32_t last = 10000;  // speak_start
    for (uint32_t now = 10000; now < 70000; now += 500) {
        TEST_ASSERT_FALSE(spkwd::should_stop(true, last, now, kTimeout));
        last = now;  // 音声フレームを受けた
    }
    // 最後の音声から 5 s 空くと止める。
    TEST_ASSERT_FALSE(spkwd::should_stop(true, last, last + kTimeout - 1, kTimeout));
    TEST_ASSERT_TRUE(spkwd::should_stop(true, last, last + kTimeout, kTimeout));
}

// millis() の折り返し (2^32 ms) をまたいでも経過時間で判定する。
static void test_millis_wraparound(void) {
    const uint32_t last = 0xFFFFF000u;  // 折り返しの 4096 ms 前
    TEST_ASSERT_EQUAL_UINT32(4096 + 100, spkwd::elapsed_ms(last, 100u));
    TEST_ASSERT_FALSE(spkwd::should_stop(true, last, 100u, kTimeout));  // 4196 ms
    TEST_ASSERT_FALSE(spkwd::should_stop(true, last, 903u, kTimeout));  // 4999 ms
    TEST_ASSERT_TRUE(spkwd::should_stop(true, last, 904u, kTimeout));   // 5000 ms
    TEST_ASSERT_TRUE(spkwd::should_stop(true, last, 20000u, kTimeout));
}

static void test_zero_timeout_stops_immediately(void) {
    TEST_ASSERT_TRUE(spkwd::should_stop(true, 1234, 1234, 0));
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_default_timeout_is_5000);
    RUN_TEST(test_not_speaking_never_stops);
    RUN_TEST(test_within_timeout_keeps_speaking);
    RUN_TEST(test_timeout_boundary_stops);
    RUN_TEST(test_audio_frames_restart_timer);
    RUN_TEST(test_millis_wraparound);
    RUN_TEST(test_zero_timeout_stops_immediately);
    return UNITY_END();
}
