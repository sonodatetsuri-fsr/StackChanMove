// ---------------------------------------------------------------------------
// ws_reassembly.h の単体テスト。
// ホスト実行: `pio test -e native`
// ---------------------------------------------------------------------------
#include <unity.h>

#include <cstdlib>
#include <cstring>
#include <vector>

#include "ws_reassembly.h"

static constexpr size_t kInline = 16;
static constexpr size_t kMaxFrame = 100000;

struct Rec {
    uint8_t op;
    std::vector<uint8_t> data;
    bool nul_ok;  // テキストのとき data[len] == 0 だったか
};

static std::vector<Rec> g_frames;
static int g_allocs = 0;
static int g_frees = 0;
static bool g_fail_alloc = false;

static void* test_alloc(size_t n) {
    if (g_fail_alloc) return nullptr;
    ++g_allocs;
    return malloc(n);
}

static void test_free(void* p) {
    if (!p) return;
    ++g_frees;
    free(p);
}

static void on_frame(void*, uint8_t op, const uint8_t* data, size_t len) {
    Rec r;
    r.op = op;
    r.data.assign(data, data + len);
    r.nul_ok = (op != wsr::kOpText) || data[len] == 0;
    g_frames.push_back(r);
}

typedef wsr::Reassembler<kInline> R;

static void init(R& r) {
    r.init(kMaxFrame, test_alloc, test_free, on_frame, nullptr);
}

static std::vector<uint8_t> pattern(size_t n) {
    std::vector<uint8_t> v(n);
    for (size_t i = 0; i < n; ++i) v[i] = static_cast<uint8_t>(i * 7 + 3);
    return v;
}

// n バイトを chunk ずつの断片として feed する。
static void feed_chunks(R& r, uint8_t op, const std::vector<uint8_t>& v, size_t chunk) {
    for (size_t off = 0; off < v.size() || off == 0; off += chunk) {
        size_t n = v.size() - off < chunk ? v.size() - off : chunk;
        r.feed(op, v.data() + off, n, v.size(), off);
        if (v.empty()) break;
    }
}

void setUp(void) {
    g_frames.clear();
    g_allocs = 0;
    g_frees = 0;
    g_fail_alloc = false;
}
void tearDown(void) {}

// ---------------------------------------------------------------------------
// 1 断片で完結
// ---------------------------------------------------------------------------
static void test_single_text_inline(void) {
    R r;
    init(r);
    const char* s = "hello";
    r.feed(wsr::kOpText, reinterpret_cast<const uint8_t*>(s), 5, 5, 0);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frames);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frags);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_EQUAL_UINT8(wsr::kOpText, g_frames[0].op);
    TEST_ASSERT_EQUAL_size_t(5, g_frames[0].data.size());
    TEST_ASSERT_EQUAL_MEMORY(s, g_frames[0].data.data(), 5);
    TEST_ASSERT_TRUE(g_frames[0].nul_ok);
    TEST_ASSERT_EQUAL_INT(0, g_allocs);  // InlineMax 以下は確保しない
}

static void test_single_text_inline_exact_max(void) {
    R r;
    init(r);
    std::vector<uint8_t> v(kInline, 'a');
    r.feed(wsr::kOpText, v.data(), v.size(), v.size(), 0);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_TRUE(g_frames[0].nul_ok);
    TEST_ASSERT_TRUE(g_frames[0].data == v);
    TEST_ASSERT_EQUAL_INT(0, g_allocs);
}

static void test_single_text_over_inline(void) {
    R r;
    init(r);
    std::vector<uint8_t> v(kInline + 50, 'b');
    r.feed(wsr::kOpText, v.data(), v.size(), v.size(), 0);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frames);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_TRUE(g_frames[0].nul_ok);
    TEST_ASSERT_TRUE(g_frames[0].data == v);
    TEST_ASSERT_EQUAL_INT(1, g_allocs);
    TEST_ASSERT_EQUAL_INT(1, g_frees);
}

static void test_single_binary(void) {
    R r;
    init(r);
    std::vector<uint8_t> small = pattern(8);
    std::vector<uint8_t> big = pattern(kInline + 100);
    r.feed(wsr::kOpBinary, small.data(), small.size(), small.size(), 0);
    r.feed(wsr::kOpBinary, big.data(), big.size(), big.size(), 0);
    TEST_ASSERT_EQUAL_UINT32(2, r.stats.frames);
    TEST_ASSERT_EQUAL_size_t(2, g_frames.size());
    TEST_ASSERT_EQUAL_UINT8(wsr::kOpBinary, g_frames[0].op);
    TEST_ASSERT_TRUE(g_frames[0].data == small);
    TEST_ASSERT_TRUE(g_frames[1].data == big);
    TEST_ASSERT_EQUAL_INT(0, g_allocs);  // バイナリ単断片は複写しない
}

// ---------------------------------------------------------------------------
// 複数断片
// ---------------------------------------------------------------------------
static void test_multi_fragment_binary_64000(void) {
    R r;
    init(r);
    std::vector<uint8_t> v = pattern(64000);
    feed_chunks(r, wsr::kOpBinary, v, 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frames);
    TEST_ASSERT_EQUAL_UINT32(16, r.stats.frags);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.desync);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_EQUAL_UINT8(wsr::kOpBinary, g_frames[0].op);
    TEST_ASSERT_EQUAL_size_t(64000, g_frames[0].data.size());
    TEST_ASSERT_TRUE(g_frames[0].data == v);
    TEST_ASSERT_FALSE(r.active);
}

static void test_multi_fragment_text_nul(void) {
    R r;
    init(r);
    std::vector<uint8_t> v(10000, 'x');
    feed_chunks(r, wsr::kOpText, v, 4096);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_TRUE(g_frames[0].nul_ok);
    TEST_ASSERT_TRUE(g_frames[0].data == v);
}

// ---------------------------------------------------------------------------
// 上限超過
// ---------------------------------------------------------------------------
static void test_oversize_dropped_then_recovers(void) {
    R r;
    init(r);
    std::vector<uint8_t> big = pattern(kMaxFrame + 1);
    feed_chunks(r, wsr::kOpBinary, big, 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.oversize);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.frames);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.desync);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frags);  // 残りの断片は数えず無視
    TEST_ASSERT_EQUAL_size_t(0, g_frames.size());
    TEST_ASSERT_EQUAL_INT(0, g_allocs);

    std::vector<uint8_t> ok = pattern(9000);
    feed_chunks(r, wsr::kOpBinary, ok, 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frames);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.oversize);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_TRUE(g_frames[0].data == ok);
}

static void test_exactly_max_frame_accepted(void) {
    R r;
    init(r);
    std::vector<uint8_t> v = pattern(kMaxFrame);
    feed_chunks(r, wsr::kOpBinary, v, 4096);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.oversize);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_TRUE(g_frames[0].data == v);
}

// ---------------------------------------------------------------------------
// オフセットずれ
// ---------------------------------------------------------------------------
static void test_offset_gap_dropped_then_recovers(void) {
    R r;
    init(r);
    std::vector<uint8_t> v = pattern(12000);
    // 先頭 (0..4095) の次に 8192.. が来た (4096.. を取りこぼし)
    r.feed(wsr::kOpBinary, v.data(), 4096, v.size(), 0);
    r.feed(wsr::kOpBinary, v.data() + 8192, 3808, v.size(), 8192);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.desync);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.frames);
    TEST_ASSERT_EQUAL_size_t(0, g_frames.size());
    TEST_ASSERT_FALSE(r.active);
    TEST_ASSERT_EQUAL_INT(g_allocs, g_frees);

    // 同じフレームの残り断片は無視される (desync は増えない)
    r.feed(wsr::kOpBinary, v.data() + 4096, 4096, v.size(), 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.desync);

    // 次の offset==0 から回復
    std::vector<uint8_t> ok = pattern(9000);
    feed_chunks(r, wsr::kOpBinary, ok, 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frames);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_TRUE(g_frames[0].data == ok);
}

static void test_missing_head_counts_desync(void) {
    R r;
    init(r);
    std::vector<uint8_t> v = pattern(8000);
    r.feed(wsr::kOpBinary, v.data() + 4096, 3904, v.size(), 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.desync);
    TEST_ASSERT_EQUAL_size_t(0, g_frames.size());
}

static void test_new_frame_during_incomplete_discards_old(void) {
    R r;
    init(r);
    std::vector<uint8_t> v = pattern(8000);
    r.feed(wsr::kOpBinary, v.data(), 4096, v.size(), 0);
    std::vector<uint8_t> ok = pattern(5000);
    feed_chunks(r, wsr::kOpBinary, ok, 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.desync);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frames);
    TEST_ASSERT_TRUE(g_frames[0].data == ok);
    TEST_ASSERT_EQUAL_INT(g_allocs, g_frees);
}

// ---------------------------------------------------------------------------
// 制御フレーム / 継続フレーム
// ---------------------------------------------------------------------------
static void test_control_frames_ignored(void) {
    R r;
    init(r);
    const uint8_t d[4] = {1, 2, 3, 4};
    r.feed(0x8, d, 2, 2, 0);
    r.feed(0x9, d, 4, 4, 0);
    r.feed(0xA, d, 4, 4, 0);
    TEST_ASSERT_EQUAL_size_t(0, g_frames.size());
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.desync);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.frames);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.frags);

    // 直後のデータフレームは通常どおり
    r.feed(wsr::kOpBinary, d, 4, 4, 0);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
}

static void test_continuation_frame_ignored_with_desync(void) {
    R r;
    init(r);
    const uint8_t d[4] = {1, 2, 3, 4};
    r.feed(wsr::kOpCont, d, 4, 4, 0);
    TEST_ASSERT_EQUAL_size_t(0, g_frames.size());
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.desync);

    r.feed(wsr::kOpBinary, d, 4, 4, 0);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.desync);
}

// ---------------------------------------------------------------------------
// アロケータ失敗 / リーク
// ---------------------------------------------------------------------------
static void test_alloc_failure_then_recovers(void) {
    R r;
    init(r);
    std::vector<uint8_t> v = pattern(9000);
    g_fail_alloc = true;
    feed_chunks(r, wsr::kOpBinary, v, 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.nomem);
    TEST_ASSERT_EQUAL_UINT32(0, r.stats.frames);
    TEST_ASSERT_EQUAL_size_t(0, g_frames.size());

    // 大きい単断片テキスト (複写用の確保) も nomem
    std::vector<uint8_t> t(kInline + 10, 'c');
    r.feed(wsr::kOpText, t.data(), t.size(), t.size(), 0);
    TEST_ASSERT_EQUAL_UINT32(2, r.stats.nomem);
    TEST_ASSERT_EQUAL_size_t(0, g_frames.size());

    g_fail_alloc = false;
    feed_chunks(r, wsr::kOpBinary, v, 4096);
    TEST_ASSERT_EQUAL_UINT32(1, r.stats.frames);
    TEST_ASSERT_EQUAL_size_t(1, g_frames.size());
    TEST_ASSERT_TRUE(g_frames[0].data == v);
}

static void test_no_leak(void) {
    R r;
    init(r);
    std::vector<uint8_t> v = pattern(20000);
    feed_chunks(r, wsr::kOpBinary, v, 4096);                           // 完走
    feed_chunks(r, wsr::kOpText, std::vector<uint8_t>(100, 'z'), 30);  // 完走 (テキスト)
    std::vector<uint8_t> t(kInline + 20, 'q');
    r.feed(wsr::kOpText, t.data(), t.size(), t.size(), 0);         // 単断片 (複写確保)
    r.feed(wsr::kOpBinary, v.data(), 4096, v.size(), 0);           // 途中
    r.feed(wsr::kOpBinary, v.data() + 9000, 100, v.size(), 9000);  // desync で破棄
    r.feed(wsr::kOpBinary, v.data(), 4096, v.size(), 0);           // 途中のまま
    TEST_ASSERT_TRUE(g_allocs > g_frees);                          // 組み立て中は保持
    r.reset();                                                     // 切断相当
    TEST_ASSERT_EQUAL_INT(g_allocs, g_frees);
    TEST_ASSERT_EQUAL_UINT32(3, r.stats.frames);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_single_text_inline);
    RUN_TEST(test_single_text_inline_exact_max);
    RUN_TEST(test_single_text_over_inline);
    RUN_TEST(test_single_binary);
    RUN_TEST(test_multi_fragment_binary_64000);
    RUN_TEST(test_multi_fragment_text_nul);
    RUN_TEST(test_oversize_dropped_then_recovers);
    RUN_TEST(test_exactly_max_frame_accepted);
    RUN_TEST(test_offset_gap_dropped_then_recovers);
    RUN_TEST(test_missing_head_counts_desync);
    RUN_TEST(test_new_frame_during_incomplete_discards_old);
    RUN_TEST(test_control_frames_ignored);
    RUN_TEST(test_continuation_frame_ignored_with_desync);
    RUN_TEST(test_alloc_failure_then_recovers);
    RUN_TEST(test_no_leak);
    return UNITY_END();
}
