// ---------------------------------------------------------------------------
// joy_protocol.h / joy_math.h の単体テスト。
// ホスト実行: `pio test -e native`
// ---------------------------------------------------------------------------
#include <unity.h>

#include <cstring>

#include "joy_math.h"
#include "joy_protocol.h"

static constexpr float kEps = 1e-5f;

void setUp(void) {}
void tearDown(void) {}

// ---------------------------------------------------------------------------
// CRC-8 (poly 0x07, init 0x00)
// ---------------------------------------------------------------------------
static void test_crc8_known_vector(void) {
    // CRC-8 の標準チェック値: "123456789" → 0xF4
    const uint8_t data[9] = {'1', '2', '3', '4', '5', '6', '7', '8', '9'};
    TEST_ASSERT_EQUAL_UINT8(0xF4, joyp::crc8(data, sizeof(data)));
    TEST_ASSERT_EQUAL_UINT8(0x00, joyp::crc8(data, 0));
}

// ---------------------------------------------------------------------------
// encode / decode
// ---------------------------------------------------------------------------
static joyp::Packet make_packet() {
    joyp::Packet p;
    p.type = joyp::TYPE_JOY;
    p.seq = 0xBEEF;
    p.x = -1000;
    p.y = 750;
    p.btn = 0x01;
    p.flags = joyp::FLAG_CALIBRATED | joyp::FLAG_LOW_BAT;
    p.aux = 3987;
    p.chan = 11;
    return p;
}

static void test_encode_layout(void) {
    const joyp::Packet p = make_packet();
    uint8_t buf[joyp::kPacketLen];
    joyp::encode(p, buf);
    TEST_ASSERT_EQUAL_UINT8('S', buf[0]);
    TEST_ASSERT_EQUAL_UINT8('J', buf[1]);
    TEST_ASSERT_EQUAL_UINT8(1, buf[2]);
    TEST_ASSERT_EQUAL_UINT8(joyp::TYPE_JOY, buf[3]);
    TEST_ASSERT_EQUAL_UINT8(0xEF, buf[4]);  // seq はリトルエンディアン
    TEST_ASSERT_EQUAL_UINT8(0xBE, buf[5]);
    TEST_ASSERT_EQUAL_UINT8(11, buf[14]);
    TEST_ASSERT_EQUAL_UINT8(joyp::crc8(buf, joyp::kPacketLen - 1), buf[15]);
}

static void test_encode_decode_roundtrip(void) {
    const joyp::Packet p = make_packet();
    uint8_t buf[joyp::kPacketLen];
    joyp::encode(p, buf);

    joyp::Packet d;
    TEST_ASSERT_TRUE(joyp::decode(buf, sizeof(buf), &d));
    TEST_ASSERT_EQUAL_UINT8(p.type, d.type);
    TEST_ASSERT_EQUAL_UINT16(p.seq, d.seq);
    TEST_ASSERT_EQUAL_INT16(p.x, d.x);
    TEST_ASSERT_EQUAL_INT16(p.y, d.y);
    TEST_ASSERT_EQUAL_UINT8(p.btn, d.btn);
    TEST_ASSERT_EQUAL_UINT8(p.flags, d.flags);
    TEST_ASSERT_EQUAL_UINT16(p.aux, d.aux);
    TEST_ASSERT_EQUAL_UINT8(p.chan, d.chan);
}

static void test_decode_rejects_corruption(void) {
    const joyp::Packet p = make_packet();
    uint8_t buf[joyp::kPacketLen];
    joyp::encode(p, buf);
    joyp::Packet d;

    // 長さ違い
    TEST_ASSERT_FALSE(joyp::decode(buf, joyp::kPacketLen - 1, &d));
    TEST_ASSERT_FALSE(joyp::decode(buf, joyp::kPacketLen + 1, &d));
    TEST_ASSERT_FALSE(joyp::decode(nullptr, joyp::kPacketLen, &d));
    TEST_ASSERT_FALSE(joyp::decode(buf, joyp::kPacketLen, nullptr));

    // crc 破壊
    uint8_t bad[joyp::kPacketLen];
    memcpy(bad, buf, sizeof(bad));
    bad[15] ^= 0xFF;
    TEST_ASSERT_FALSE(joyp::decode(bad, sizeof(bad), &d));

    // ペイロード破壊 (crc はそのまま)
    memcpy(bad, buf, sizeof(bad));
    bad[8] ^= 0x01;
    TEST_ASSERT_FALSE(joyp::decode(bad, sizeof(bad), &d));

    // magic 破壊
    memcpy(bad, buf, sizeof(bad));
    bad[0] = 'X';
    bad[15] = joyp::crc8(bad, joyp::kPacketLen - 1);
    TEST_ASSERT_FALSE(joyp::decode(bad, sizeof(bad), &d));

    // バージョン違い
    memcpy(bad, buf, sizeof(bad));
    bad[2] = 2;
    bad[15] = joyp::crc8(bad, joyp::kPacketLen - 1);
    TEST_ASSERT_FALSE(joyp::decode(bad, sizeof(bad), &d));

    // 無傷なら通る (上のテストが常に false になっていないことの確認)
    TEST_ASSERT_TRUE(joyp::decode(buf, sizeof(buf), &d));
}

// ---------------------------------------------------------------------------
// 軸のワイヤ変換 / ペアリングキー
// ---------------------------------------------------------------------------
static void test_axis_wire(void) {
    TEST_ASSERT_EQUAL_INT16(1000, joyp::axis_to_wire(1.0f));
    TEST_ASSERT_EQUAL_INT16(-1000, joyp::axis_to_wire(-1.0f));
    TEST_ASSERT_EQUAL_INT16(1000, joyp::axis_to_wire(2.5f));    // クランプ
    TEST_ASSERT_EQUAL_INT16(-1000, joyp::axis_to_wire(-2.5f));  // クランプ
    TEST_ASSERT_EQUAL_INT16(0, joyp::axis_to_wire(0.0f));
    TEST_ASSERT_EQUAL_INT16(500, joyp::axis_to_wire(0.5f));
    TEST_ASSERT_EQUAL_INT16(-123, joyp::axis_to_wire(-0.1234f));  // 四捨五入

    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.5f, joyp::axis_from_wire(500));
    TEST_ASSERT_FLOAT_WITHIN(kEps, -1.0f, joyp::axis_from_wire(-1000));
    TEST_ASSERT_FLOAT_WITHIN(kEps, 1.0f, joyp::axis_from_wire(32000));  // クランプ
    TEST_ASSERT_FLOAT_WITHIN(1e-3f, 0.75f, joyp::axis_from_wire(joyp::axis_to_wire(0.75f)));
}

static void test_pair_key_low16(void) {
    TEST_ASSERT_EQUAL_UINT16(0x434A, joyp::pair_key_low16(0x5354434Au));
    TEST_ASSERT_EQUAL_UINT16(0xFFFF, joyp::pair_key_low16(0xFFFFFFFFu));
}

// ---------------------------------------------------------------------------
// seq の単調性 (u16 ラップ)
// ---------------------------------------------------------------------------
static void test_seq_is_newer(void) {
    TEST_ASSERT_TRUE(joyp::seq_is_newer(1, 0));
    TEST_ASSERT_TRUE(joyp::seq_is_newer(100, 1));
    TEST_ASSERT_FALSE(joyp::seq_is_newer(0, 0));      // 重複
    TEST_ASSERT_FALSE(joyp::seq_is_newer(5, 7));      // 順序逆転
    TEST_ASSERT_TRUE(joyp::seq_is_newer(0, 0xFFFF));  // ラップ
    TEST_ASSERT_TRUE(joyp::seq_is_newer(3, 0xFFFE));
    TEST_ASSERT_FALSE(joyp::seq_is_newer(0xFFFE, 3));
    // 半周以上離れた値は「古い」とみなす (再起動の巻き戻りは相手側で seq=0 から)
    TEST_ASSERT_FALSE(joyp::seq_is_newer(40000, 0));
}

static void test_seq_gap(void) {
    TEST_ASSERT_EQUAL_UINT16(0, joyp::seq_gap(1, 0));       // 連番 = 欠落なし
    TEST_ASSERT_EQUAL_UINT16(2, joyp::seq_gap(10, 7));      // 8, 9 が欠落
    TEST_ASSERT_EQUAL_UINT16(0, joyp::seq_gap(0, 0xFFFF));  // ラップ直後
    TEST_ASSERT_EQUAL_UINT16(3, joyp::seq_gap(2, 0xFFFE));  // ラップを跨いだ欠落
    TEST_ASSERT_EQUAL_UINT16(0, joyp::seq_gap(5, 5));
}

// ---------------------------------------------------------------------------
// joy_math: デッドゾーン / エクスポ
// ---------------------------------------------------------------------------
static void test_shape_axes_deadzone(void) {
    float x = 0.05f, y = 0.05f;  // r ≈ 0.0707 < 0.10
    joym::shape_axes(&x, &y, 0.10f, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, x);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, y);

    x = 0.0f;
    y = 0.099f;
    joym::shape_axes(&x, &y, 0.10f, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, y);
}

static void test_shape_axes_continuous_above_deadzone(void) {
    // デッドゾーン直上では出力がほぼ 0 から連続に立ち上がる (跳ばない)。
    float x = 0.0f, y = 0.1001f;
    joym::shape_axes(&x, &y, 0.10f, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, x);
    TEST_ASSERT_TRUE(y > 0.0f);
    TEST_ASSERT_TRUE(y < 0.001f);

    // 全倒しは 1.0 のまま (再スケール後も上限に届く)。
    x = 0.0f;
    y = 1.0f;
    joym::shape_axes(&x, &y, 0.10f, 0.0f);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 1.0f, y);
}

static void test_shape_axes_expo(void) {
    // deadzone 0 / expo 0.3: x' = 0.7x + 0.3x³
    float x = 0.5f, y = 0.0f;
    joym::shape_axes(&x, &y, 0.0f, 0.3f);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 0.7f * 0.5f + 0.3f * 0.125f, x);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, y);

    // 端点は保存され、符号は反転しない。
    x = -1.0f;
    y = 1.0f;
    joym::shape_axes(&x, &y, 0.0f, 0.3f);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, -1.0f, x);
    TEST_ASSERT_FLOAT_WITHIN(1e-4f, 1.0f, y);
}

static void test_axis_from_pos(void) {
    // MiniJoyC の POS は符号付き 10 bit (−512..511、中立 0)。
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, joym::axis_from_pos(0, 0.0f));
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.5f, joym::axis_from_pos(256, 0.0f));
    TEST_ASSERT_FLOAT_WITHIN(kEps, -1.0f, joym::axis_from_pos(-512, 0.0f));
    TEST_ASSERT_FLOAT_WITHIN(kEps, 511.0f / 512.0f, joym::axis_from_pos(511, 0.0f));
    TEST_ASSERT_FLOAT_WITHIN(kEps, 1.0f, joym::axis_from_pos(700, 0.0f));  // クランプ
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, joym::axis_from_pos(0, 0.0f, 0.0f));
    // 実機で観測した中立 −18 (uint16 では 65518) を int16 として扱えば中央校正で 0 になる
    const int16_t neutral = static_cast<int16_t>(static_cast<uint16_t>(65518));
    TEST_ASSERT_EQUAL_INT16(-18, neutral);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, joym::axis_from_pos(neutral, -18.0f));
    TEST_ASSERT_FLOAT_WITHIN(kEps, 5.0f / 512.0f, joym::axis_from_pos(-13, -18.0f));
}

// ---------------------------------------------------------------------------
// joy_math: (v, w) 写像
// ---------------------------------------------------------------------------
static constexpr float kMaxV = 0.15f;
static constexpr float kMaxW = 2.0f;

static void test_stick_to_cmd_straight(void) {
    float v = 0.0f, w = 0.0f;
    joym::stick_to_cmd(0.0f, 1.0f, kMaxV, kMaxW, &v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, kMaxV, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, w);

    joym::stick_to_cmd(0.0f, -1.0f, kMaxV, kMaxW, &v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, -kMaxV, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, w);
}

static void test_stick_to_cmd_spin(void) {
    // x のみ = 超信地旋回 (v = 0)。x 左正 → w 正 (前方が左を向く)。
    float v = 0.0f, w = 0.0f;
    joym::stick_to_cmd(1.0f, 0.0f, kMaxV, kMaxW, &v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, kMaxW, w);

    joym::stick_to_cmd(-1.0f, 0.0f, kMaxV, kMaxW, &v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.0f, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, -kMaxW, w);
}

static void test_stick_to_cmd_arc_and_reverse(void) {
    float v = 0.0f, w = 0.0f;
    joym::stick_to_cmd(0.5f, 0.5f, kMaxV, kMaxW, &v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.5f * kMaxV, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, 0.5f * kMaxW, w);

    // 後退中も w の符号は x のまま (y=0 付近で反転しない)。
    joym::stick_to_cmd(1.0f, -1.0f, kMaxV, kMaxW, &v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, -kMaxV, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, kMaxW, w);

    // 範囲外入力はクランプされる。
    joym::stick_to_cmd(3.0f, -4.0f, kMaxV, kMaxW, &v, &w);
    TEST_ASSERT_FLOAT_WITHIN(kEps, -kMaxV, v);
    TEST_ASSERT_FLOAT_WITHIN(kEps, kMaxW, w);
}

static void test_derive_key_deterministic(void) {
    uint8_t a[joyp::kKeyLen];
    uint8_t b[joyp::kKeyLen];
    joyp::derive_key(0x5354434Au, a);
    joyp::derive_key(0x5354434Au, b);
    TEST_ASSERT_EQUAL_UINT8_ARRAY(a, b, joyp::kKeyLen);
    // 下位バイトから並び、i を XOR する (両ファームが共有する規則)
    TEST_ASSERT_EQUAL_UINT8(0x4A ^ 0, a[0]);
    TEST_ASSERT_EQUAL_UINT8(0x43 ^ 1, a[1]);
    TEST_ASSERT_EQUAL_UINT8(0x54 ^ 2, a[2]);
    TEST_ASSERT_EQUAL_UINT8(0x53 ^ 3, a[3]);
    TEST_ASSERT_EQUAL_UINT8(0x4A ^ 4, a[4]);
    uint8_t c[joyp::kKeyLen];
    joyp::derive_key(0x5354434Bu, c);
    TEST_ASSERT_NOT_EQUAL(a[0], c[0]);
}

int main(int, char**) {
    UNITY_BEGIN();
    RUN_TEST(test_crc8_known_vector);
    RUN_TEST(test_encode_layout);
    RUN_TEST(test_encode_decode_roundtrip);
    RUN_TEST(test_decode_rejects_corruption);
    RUN_TEST(test_axis_wire);
    RUN_TEST(test_pair_key_low16);
    RUN_TEST(test_seq_is_newer);
    RUN_TEST(test_seq_gap);
    RUN_TEST(test_shape_axes_deadzone);
    RUN_TEST(test_shape_axes_continuous_above_deadzone);
    RUN_TEST(test_shape_axes_expo);
    RUN_TEST(test_axis_from_pos);
    RUN_TEST(test_stick_to_cmd_straight);
    RUN_TEST(test_stick_to_cmd_spin);
    RUN_TEST(test_stick_to_cmd_arc_and_reverse);
    RUN_TEST(test_derive_key_deterministic);
    return UNITY_END();
}
