#pragma once
// ---------------------------------------------------------------------------
// joy_protocol.h — MiniJoyC リモコン ⇄ StackChanMove の ESP-NOW パケット
//
// firmware/ (CoreS3 受信側) と firmware_remote/ (StickC Plus 送信側) の両方から
// include する。Arduino / ESP-IDF に依存しない純粋な C++ (header-only) で、
// firmware の native テスト (`pio test -e native`) でも検証する。
//
// すべてリトルエンディアン、固定長 16 バイト:
//   0  magic[2]  'S','J'
//   2  ver       = 1
//   3  type      0x01 JOY (リモコン→端末) / 0x02 DISCOVER (リモコン→ブロードキャスト)
//                / 0x03 ACK (端末→リモコン)
//   4  seq u16   送信連番 (重複・欠落検出)
//   6  x  i16    正規化 x × 1000 (−1000..1000、左 +)
//   8  y  i16    正規化 y × 1000 (前 +)
//  10  btn u8    bit0 = 押下中
//  11  flags u8  bit0 = 校正済み、bit1 = 低電圧
//  12  aux u16   JOY: バッテリ電圧 [mV] / DISCOVER・ACK: JOY_PAIR_KEY の下位 16 bit
//  14  chan u8   ACK: 端末の現在チャネル / それ以外 0
//  15  crc8      offset 0..14 の CRC-8 (poly 0x07, init 0x00)
// ---------------------------------------------------------------------------

#include <cstddef>
#include <cstdint>

namespace joyp {

constexpr uint8_t kMagic0 = 'S';
constexpr uint8_t kMagic1 = 'J';
constexpr uint8_t kVersion = 1;
constexpr size_t kPacketLen = 16;

enum Type : uint8_t {
    TYPE_JOY = 0x01,
    TYPE_DISCOVER = 0x02,
    TYPE_ACK = 0x03,
};

enum Flags : uint8_t {
    FLAG_CALIBRATED = 0x01,
    FLAG_LOW_BAT = 0x02,
};

constexpr int16_t kAxisScale = 1000;  // 正規化値 [-1, 1] × 1000

/// デコード済み / エンコード前のパケット内容 (magic / ver / crc は含まない)。
struct Packet {
    uint8_t type = TYPE_JOY;
    uint16_t seq = 0;
    int16_t x = 0;  // × 1000
    int16_t y = 0;  // × 1000
    uint8_t btn = 0;
    uint8_t flags = 0;
    uint16_t aux = 0;
    uint8_t chan = 0;
};

/// CRC-8 (poly 0x07, init 0x00, 非反転)。
inline uint8_t crc8(const uint8_t* data, size_t len) {
    uint8_t crc = 0x00;
    for (size_t i = 0; i < len; ++i) {
        crc ^= data[i];
        for (int b = 0; b < 8; ++b) {
            crc = static_cast<uint8_t>((crc & 0x80) ? ((crc << 1) ^ 0x07) : (crc << 1));
        }
    }
    return crc;
}

/// 正規化値 [-1, 1] → i16 (× 1000)。範囲外はクランプ。
inline int16_t axis_to_wire(float v) {
    if (v > 1.0f) v = 1.0f;
    if (v < -1.0f) v = -1.0f;
    const float s = v * static_cast<float>(kAxisScale);
    return static_cast<int16_t>(s >= 0.0f ? s + 0.5f : s - 0.5f);
}

/// i16 (× 1000) → 正規化値 [-1, 1]。範囲外はクランプ。
inline float axis_from_wire(int16_t raw) {
    float v = static_cast<float>(raw) / static_cast<float>(kAxisScale);
    if (v > 1.0f) v = 1.0f;
    if (v < -1.0f) v = -1.0f;
    return v;
}

/// JOY_PAIR_KEY (u32) のうち DISCOVER / ACK の aux に載せる下位 16 bit。
inline uint16_t pair_key_low16(uint32_t key) {
    return static_cast<uint16_t>(key & 0xFFFFu);
}

constexpr size_t kKeyLen = 16;  // ESP_NOW_KEY_LEN

/// JOY_ESPNOW_ENCRYPT 用: JOY_PAIR_KEY から 16 バイトの PMK / LMK を作る。
/// 両ファームが必ずこの関数を使い、導出規則を一致させる。
inline void derive_key(uint32_t key, uint8_t* out) {
    for (size_t i = 0; i < kKeyLen; ++i) {
        out[i] = static_cast<uint8_t>((key >> (8 * (i % 4))) & 0xFFu) ^ static_cast<uint8_t>(i);
    }
}

/// パケットを 16 バイトにエンコードする (crc 付き)。out は kPacketLen 以上。
inline void encode(const Packet& p, uint8_t* out) {
    out[0] = kMagic0;
    out[1] = kMagic1;
    out[2] = kVersion;
    out[3] = p.type;
    out[4] = static_cast<uint8_t>(p.seq & 0xFF);
    out[5] = static_cast<uint8_t>((p.seq >> 8) & 0xFF);
    const uint16_t ux = static_cast<uint16_t>(p.x);
    out[6] = static_cast<uint8_t>(ux & 0xFF);
    out[7] = static_cast<uint8_t>((ux >> 8) & 0xFF);
    const uint16_t uy = static_cast<uint16_t>(p.y);
    out[8] = static_cast<uint8_t>(uy & 0xFF);
    out[9] = static_cast<uint8_t>((uy >> 8) & 0xFF);
    out[10] = p.btn;
    out[11] = p.flags;
    out[12] = static_cast<uint8_t>(p.aux & 0xFF);
    out[13] = static_cast<uint8_t>((p.aux >> 8) & 0xFF);
    out[14] = p.chan;
    out[15] = crc8(out, kPacketLen - 1);
}

/// 受信バイト列を検証してデコードする。長さ / magic / ver / crc のいずれかが
/// 不一致なら false (out は変更しない)。type の妥当性は呼び出し側で判定する。
inline bool decode(const uint8_t* in, size_t len, Packet* out) {
    if (!in || !out || len != kPacketLen) return false;
    if (in[0] != kMagic0 || in[1] != kMagic1) return false;
    if (in[2] != kVersion) return false;
    if (crc8(in, kPacketLen - 1) != in[15]) return false;
    out->type = in[3];
    out->seq = static_cast<uint16_t>(in[4] | (in[5] << 8));
    out->x = static_cast<int16_t>(static_cast<uint16_t>(in[6] | (in[7] << 8)));
    out->y = static_cast<int16_t>(static_cast<uint16_t>(in[8] | (in[9] << 8)));
    out->btn = in[10];
    out->flags = in[11];
    out->aux = static_cast<uint16_t>(in[12] | (in[13] << 8));
    out->chan = in[14];
    return true;
}

/// seq の前進判定 (u16 ラップ考慮)。prev より新しければ true。
/// 差が 32768 以上離れているときは巻き戻り (再起動) とみなして true を返す。
inline bool seq_is_newer(uint16_t seq, uint16_t prev) {
    const uint16_t diff = static_cast<uint16_t>(seq - prev);
    return diff != 0 && diff < 0x8000u;
}

/// seq_is_newer() が true のときに欠落した個数 (seq − prev − 1)。
inline uint16_t seq_gap(uint16_t seq, uint16_t prev) {
    const uint16_t diff = static_cast<uint16_t>(seq - prev);
    return diff == 0 ? 0 : static_cast<uint16_t>(diff - 1);
}

}  // namespace joyp
