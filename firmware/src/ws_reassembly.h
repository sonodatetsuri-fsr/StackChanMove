#pragma once
// ---------------------------------------------------------------------------
// ws_reassembly — esp_websocket_client の DATA イベント断片から 1 フレームを組み立てる。
// header-only・ESP-IDF 非依存 (native テスト可)。
//
// esp_websocket_client は buffer_size を超えるフレームを複数の DATA イベントに分けて渡す
// (payload_len = 全長、payload_offset = 断片の位置、data_len = 断片の長さ)。
// 組み立てバッファの確保・解放は呼び出し側が渡す関数で行う (実機は PSRAM、テストは malloc)。
// ---------------------------------------------------------------------------

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace wsr {

static constexpr uint8_t kOpCont = 0x0;
static constexpr uint8_t kOpText = 0x1;
static constexpr uint8_t kOpBinary = 0x2;

typedef void* (*AllocFn)(size_t n);
typedef void (*FreeFn)(void* p);
/// 完成したフレーム。テキストは data[len] == 0 (NUL 終端) が保証される。
/// data はコールバックの間だけ有効。
typedef void (*FrameFn)(void* ctx, uint8_t op, const uint8_t* data, size_t len);

struct Stats {
    uint32_t frames = 0;    // 完成したフレーム数
    uint32_t frags = 0;     // 受けた断片数 (データフレームのみ)
    uint32_t oversize = 0;  // 上限超過で捨てたフレーム
    uint32_t desync = 0;    // オフセットのずれ / 継続フレームで捨てたフレーム
    uint32_t nomem = 0;     // 確保失敗で捨てたフレーム
};

template <size_t InlineMax>
struct Reassembler {
    size_t max_frame = 0;
    AllocFn alloc = nullptr;
    FreeFn dealloc = nullptr;
    FrameFn on_frame = nullptr;
    void* ctx = nullptr;

    // 組み立て中の状態
    uint8_t* buf = nullptr;
    size_t total = 0;
    size_t filled = 0;
    uint8_t op = 0;
    bool active = false;
    bool skipping = false;  // 捨てると決めたフレームの残りの断片を読み飛ばす
    Stats stats;
    uint8_t inline_buf[InlineMax + 1];

    void init(size_t max_frame_, AllocFn alloc_, FreeFn free_, FrameFn on_frame_, void* ctx_) {
        max_frame = max_frame_;
        alloc = alloc_;
        dealloc = free_;
        on_frame = on_frame_;
        ctx = ctx_;
        reset();
    }

    /// 組み立て中のフレームを捨てる (切断時・破棄時に呼ぶ)。統計は残す。
    void reset() {
        if (buf && dealloc) dealloc(buf);
        buf = nullptr;
        total = 0;
        filled = 0;
        op = 0;
        active = false;
        skipping = false;
    }

    /// DATA イベント 1 つを渡す。
    void feed(uint8_t op_code, const uint8_t* data, size_t data_len, size_t payload_len,
              size_t payload_offset) {
        if (payload_offset == 0) {
            // 新しいフレームの開始。前のフレームが未完成ならずれとして捨てる。
            if (active) {
                ++stats.desync;
                reset();
            }
            skipping = false;
            if (op_code == kOpCont) {
                // Hub (Starlette / uvicorn) は継続フレームを送らない。
                ++stats.desync;
                skipping = true;
                return;
            }
            if (op_code != kOpText && op_code != kOpBinary) {
                // 制御フレーム (CLOSE / PING / PONG) は無視 (PONG はクライアントが返す)。
                skipping = true;
                return;
            }
            ++stats.frags;
            if (payload_len > max_frame) {
                ++stats.oversize;
                skipping = true;
                return;
            }
            if (data_len > payload_len) {
                ++stats.desync;
                skipping = true;
                return;
            }
            if (data_len == payload_len) {
                deliver_single(op_code, data, data_len);
                return;
            }
            buf = static_cast<uint8_t*>(alloc ? alloc(payload_len + 1) : nullptr);
            if (!buf) {
                ++stats.nomem;
                skipping = true;
                return;
            }
            total = payload_len;
            filled = 0;
            op = op_code;
            active = true;
            append(data, data_len);
            return;
        }

        // 続きの断片
        if (skipping) return;
        if (!active) {
            // 先頭を取りこぼした。
            ++stats.desync;
            skipping = true;
            return;
        }
        ++stats.frags;
        if (payload_offset != filled || payload_len != total || filled + data_len > total) {
            ++stats.desync;
            reset();
            skipping = true;
            return;
        }
        append(data, data_len);
    }

   private:
    void deliver_single(uint8_t op_code, const uint8_t* data, size_t len) {
        if (op_code == kOpText) {
            // NUL 終端が要る。小さければ静的バッファ、大きければ確保して複写。
            uint8_t* dst = inline_buf;
            if (len > InlineMax) {
                dst = static_cast<uint8_t*>(alloc ? alloc(len + 1) : nullptr);
                if (!dst) {
                    ++stats.nomem;
                    return;
                }
            }
            if (len) memcpy(dst, data, len);
            dst[len] = 0;
            ++stats.frames;
            if (on_frame) on_frame(ctx, op_code, dst, len);
            if (dst != inline_buf && dealloc) dealloc(dst);
            return;
        }
        ++stats.frames;
        if (on_frame) on_frame(ctx, op_code, data, len);
    }

    void append(const uint8_t* data, size_t len) {
        if (len) memcpy(buf + filled, data, len);
        filled += len;
        if (filled < total) return;
        buf[total] = 0;
        ++stats.frames;
        if (on_frame) on_frame(ctx, op, buf, total);
        reset();
    }
};

}  // namespace wsr
