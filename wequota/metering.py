"""Token 计量：把一条消息折算成 token，并记录折算明细。

设计要点：
- 每种消息类型一个明确公式，系数全部来自 MeteringConfig（可调）。
- 只复用多模态模型的「计量公式」，不跑模型推理（图片读取尺寸即可）。
- 通话双方同时输出，故乘 duplex_factor=2。
- 语音消息优先转写后按文字计；无转写模型时降级为「时长 × rate」。
- 微信语音单条最长 60 秒，所以不设时长上限（有转写器就一律转写，不按长短跳过）。
- tiktoken 不可用时降级为启发式估算，保证离线单机也能跑。
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, Protocol

from .config import MeteringConfig
from .contracts import MessageEnvelope

try:  # tiktoken 是首选计量引擎，缺了也能跑（降级）
    import tiktoken
except ImportError:  # pragma: no cover
    tiktoken = None  # type: ignore

# 随项目附带的编码表缓存，保证离线单机也能用真实 tokenizer 计量
BUNDLED_ENCODING_CACHE = Path(__file__).resolve().parent.parent / "assets" / "tiktoken_cache"


class Transcriber(Protocol):
    def transcribe(self, path: str) -> str:
        """把语音文件转成文字。"""
        ...


def _heuristic_text_tokens(text: str) -> int:
    """离线兜底估算：中文约 1 token/字，英文约 4 字符/token。"""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return max(1, int(cjk * 1.0 + other / 4))


class Meter:
    def __init__(self, config: MeteringConfig | None = None, transcriber: Transcriber | None = None):
        self.config = config or MeteringConfig()
        self.transcriber = transcriber
        self._enc = None
        self._enc_failed = False

    # ---------- 对外主入口 ----------

    def meter(self, message: MessageEnvelope) -> int:
        """计量一条消息，回填 token_count / meter_detail 并返回 token 数。"""
        tokens, detail = self.measure(message.msg_type, message.text, message.media_meta, message.media_path)
        message.token_count = tokens
        message.meter_detail = detail
        return tokens

    def measure(
        self,
        msg_type: str,
        text: str | None = None,
        media_meta: dict[str, Any] | None = None,
        media_path: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        meta = media_meta or {}
        match msg_type:
            case "text":
                return self._measure_text(text or "")
            case "voice":
                return self._measure_voice(meta, media_path)
            case "image" | "emoji":
                return self._measure_image(meta)
            case "video":
                return self._measure_video(meta)
            case "voice_call":
                return self._measure_call(meta, self.config.voice_call_rate)
            case "video_call":
                return self._measure_call(meta, self.config.video_call_rate)
            case _:
                return 0, {"formula": "non_metered", "msg_type": msg_type}

    # ---------- 各类公式 ----------

    def _encoding(self):
        """获取 tokenizer；优先用随包编码表，失败则调用方降级到启发式估算。"""
        if self._enc is not None:
            return self._enc
        if tiktoken is None or self._enc_failed:
            return None
        if BUNDLED_ENCODING_CACHE.exists():
            os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(BUNDLED_ENCODING_CACHE))
        try:
            self._enc = tiktoken.get_encoding(self.config.text_encoding)
        except Exception:  # 无网络且本地无缓存 → 降级，不能让计量停摆
            self._enc_failed = True
            return None
        return self._enc

    def _measure_text(self, text: str) -> tuple[int, dict[str, Any]]:
        enc = self._encoding()
        if enc is not None:
            tokens = len(enc.encode(text))
            engine = "tiktoken"
        else:
            tokens = _heuristic_text_tokens(text)
            engine = "heuristic"
        return tokens, {
            "engine": engine,
            "encoding": self.config.text_encoding if engine == "tiktoken" else None,
            "formula": "tokenizer(text)",
            "chars": len(text),
        }

    def _measure_voice(self, meta: dict, media_path: str | None) -> tuple[int, dict[str, Any]]:
        duration = float(meta.get("duration_s", 0) or 0)
        # 微信语音单条最长 60 秒，不设时长上限：有转写器就一律转写后再按文字计
        if self.transcriber is not None and media_path:
            try:
                transcript = self.transcriber.transcribe(media_path)
                tokens, detail = self._measure_text(transcript)
                detail.update({
                    "formula": "tokenizer(transcript)",
                    "engine": "whisper",
                    "transcript": transcript,
                    "duration_s": duration,
                })
                return tokens, detail
            except Exception as exc:  # 转写失败必须降级，不能拖垮计量
                fallback = self._duration_tokens(duration, self.config.voice_fallback_rate)
                return fallback, {
                    "formula": "duration * fallback_rate",
                    "engine": "fallback_duration",
                    "duration_s": duration,
                    "reason": f"transcribe failed: {exc}",
                }
        return self._duration_tokens(duration, self.config.voice_fallback_rate), {
            "formula": "duration * fallback_rate",
            "engine": "fallback_duration",
            "duration_s": duration,
            "reason": ("no transcriber configured" if self.transcriber is None
                       else "no media path"),
        }

    def _measure_image(self, meta: dict) -> tuple[int, dict[str, Any]]:
        w = int(meta.get("width") or self.config.default_image_width)
        h = int(meta.get("height") or self.config.default_image_height)
        size = self.config.image_tile_size
        tiles = math.ceil(w / size) * math.ceil(h / size)
        tokens = tiles * self.config.tokens_per_tile + self.config.image_base_tokens
        return tokens, {
            "formula": "tiles * tokens_per_tile + image_base_tokens",
            "engine": "vision_formula",
            "width": w, "height": h, "tile_size": size, "tiles": tiles,
        }

    def _measure_video(self, meta: dict) -> tuple[int, dict[str, Any]]:
        duration = float(meta.get("duration_s", 0) or 0)
        if duration > 0:
            frames = min(int(round(duration * self.config.video_fps)), self.config.video_max_frames)
        else:
            frames = self.config.default_video_frames
        frames = max(frames, 1)
        tokens = frames * self.config.tokens_per_video_frame
        return tokens, {
            "formula": "frames * tokens_per_video_frame",
            "engine": "vision_formula",
            "duration_s": duration, "frames": frames,
            "fps": self.config.video_fps, "max_frames": self.config.video_max_frames,
        }

    def _measure_call(self, meta: dict, rate: float) -> tuple[int, dict[str, Any]]:
        duration = float(meta.get("duration_s", 0) or 0)
        factor = self.config.call_duplex_factor
        tokens = int(round(duration * factor * rate))
        return tokens, {
            "formula": "duration * duplex_factor * rate",
            "engine": "duration_formula",
            "duration_s": duration, "duplex_factor": factor, "rate": rate,
        }

    @staticmethod
    def _duration_tokens(duration: float, rate: float) -> int:
        return int(round(duration * rate))
