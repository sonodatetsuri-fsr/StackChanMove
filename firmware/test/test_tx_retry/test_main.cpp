// ---------------------------------------------------------------------------
// tx_retry.h の単体テスト。
// ホスト実行: `pio test -e native`
// ---------------------------------------------------------------------------
#include <unity.h>

#include <cstdint>
#include <deque>
#include <string>
#include <vector>

#include "tx_retry.h"

void setUp(void) {}
void tearDown(void) {}

static void test_default_retry_max_is_3(void) {
    TEST_ASSERT_EQUAL_UINT32(3, WS_TX_RETRY_MAX);
}

static void test_sent_is_sent(void) {
    TEST_ASSERT_TRUE(txr::Action::kSent == txr::decide(true, true, 0, 3));
    // 送信が成功した後に切れても、送信済みは送信済み。
    TEST_ASSERT_TRUE(txr::Action::kSent == txr::decide(true, false, 0, 3));
    // 再送の末に成功した場合も送信済み。
    TEST_ASSERT_TRUE(txr::Action::kSent == txr::decide(true, true, 2, 3));
}

static void test_failure_while_connected_retries_until_max(void) {
    TEST_ASSERT_TRUE(txr::Action::kRetry == txr::decide(false, true, 1, 3));
    TEST_ASSERT_TRUE(txr::Action::kRetry == txr::decide(false, true, 2, 3));
    TEST_ASSERT_TRUE(txr::Action::kDrop == txr::decide(false, true, 3, 3));
    TEST_ASSERT_TRUE(txr::Action::kDrop == txr::decide(false, true, 4, 3));
}

static void test_failure_while_disconnected_drops(void) {
    TEST_ASSERT_TRUE(txr::Action::kDrop == txr::decide(false, false, 1, 3));
    TEST_ASSERT_TRUE(txr::Action::kDrop == txr::decide(false, false, 2, 3));
}

static void test_retry_max_1_drops_on_first_failure(void) {
    TEST_ASSERT_TRUE(txr::Action::kDrop == txr::decide(false, true, 1, 1));
}

// net_ctrl の使い方 (再送待ちの間は後続を送らない) を模した送信列で、順序と破棄数を確かめる。
struct Sim {
    std::deque<std::string> queue;
    std::string pending;
    bool has_pending = false;
    uint32_t pending_fail = 0;
    std::vector<std::string> sent;
    uint32_t drops = 0;
    uint32_t attempts = 0;

    // 1 件送る。send_ok(attempt) が送信結果を決める。true = 済み (送信 / 破棄)、false = 再送待ち。
    template <typename F>
    bool try_send(const std::string& msg, uint32_t* fails, bool connected, F send_ok) {
        txr::Action act = txr::Action::kDrop;
        if (connected) {
            const bool ok = send_ok(attempts++);
            if (!ok) ++(*fails);
            act = txr::decide(ok, connected, *fails, WS_TX_RETRY_MAX);
        }
        if (act == txr::Action::kRetry) return false;
        if (act == txr::Action::kSent) {
            sent.push_back(msg);
        } else {
            ++drops;
        }
        return true;
    }

    // net_ctrl_task の 1 周期。
    template <typename F>
    void cycle(bool connected, F send_ok) {
        if (has_pending) {
            if (try_send(pending, &pending_fail, connected, send_ok)) {
                has_pending = false;
                pending_fail = 0;
            }
        }
        while (!has_pending && !queue.empty()) {
            std::string m = queue.front();
            queue.pop_front();
            uint32_t fails = 0;
            if (!try_send(m, &fails, connected, send_ok)) {
                pending = m;
                has_pending = true;
                pending_fail = fails;
            }
        }
    }
};

static void test_sim_retry_keeps_order(void) {
    Sim s;
    s.queue = {"a", "b", "c"};
    // 1 回目 ("a") と 2 回目 ("a" の再送) が失敗し、以降は成功する。
    auto ok = [](uint32_t attempt) { return attempt >= 2; };
    s.cycle(true, ok);  // a 失敗 → 再送待ち、b / c は送らない
    TEST_ASSERT_TRUE(s.has_pending);
    TEST_ASSERT_EQUAL_UINT32(0, s.sent.size());
    TEST_ASSERT_EQUAL_UINT32(2, s.queue.size());
    s.cycle(true, ok);  // a 再送失敗 (2 回目)
    TEST_ASSERT_TRUE(s.has_pending);
    TEST_ASSERT_EQUAL_UINT32(2, s.pending_fail);
    s.cycle(true, ok);  // a 成功 → b, c
    TEST_ASSERT_FALSE(s.has_pending);
    TEST_ASSERT_EQUAL_UINT32(3, s.sent.size());
    TEST_ASSERT_EQUAL_STRING("a", s.sent[0].c_str());
    TEST_ASSERT_EQUAL_STRING("b", s.sent[1].c_str());
    TEST_ASSERT_EQUAL_STRING("c", s.sent[2].c_str());
    TEST_ASSERT_EQUAL_UINT32(0, s.drops);
}

static void test_sim_drop_after_retry_max(void) {
    Sim s;
    s.queue = {"a", "b"};
    // "a" の 3 回が失敗し、以降は成功する。
    auto ok = [](uint32_t attempt) { return attempt >= WS_TX_RETRY_MAX; };
    for (int i = 0; i < WS_TX_RETRY_MAX - 1; ++i) {
        s.cycle(true, ok);
        TEST_ASSERT_TRUE(s.has_pending);
        TEST_ASSERT_EQUAL_UINT32(0, s.drops);
    }
    s.cycle(true, ok);  // 3 回目の失敗で a を捨て、b を送る
    TEST_ASSERT_EQUAL_UINT32(1, s.drops);
    TEST_ASSERT_FALSE(s.has_pending);
    TEST_ASSERT_EQUAL_UINT32(1, s.sent.size());
    TEST_ASSERT_EQUAL_STRING("b", s.sent[0].c_str());
    TEST_ASSERT_EQUAL_UINT32(WS_TX_RETRY_MAX + 1, s.attempts);
}

static void test_sim_disconnect_drops_pending(void) {
    Sim s;
    s.queue = {"a"};
    auto fail = [](uint32_t) { return false; };
    s.cycle(true, fail);
    TEST_ASSERT_TRUE(s.has_pending);
    s.cycle(false, fail);  // 切れたら再送待ちを捨てる (送信は試みない)
    TEST_ASSERT_FALSE(s.has_pending);
    TEST_ASSERT_EQUAL_UINT32(1, s.drops);
    TEST_ASSERT_EQUAL_UINT32(1, s.attempts);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_default_retry_max_is_3);
    RUN_TEST(test_sent_is_sent);
    RUN_TEST(test_failure_while_connected_retries_until_max);
    RUN_TEST(test_failure_while_disconnected_drops);
    RUN_TEST(test_retry_max_1_drops_on_first_failure);
    RUN_TEST(test_sim_retry_keeps_order);
    RUN_TEST(test_sim_drop_after_retry_max);
    RUN_TEST(test_sim_disconnect_drops_pending);
    return UNITY_END();
}
