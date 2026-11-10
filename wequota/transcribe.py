"""M6：本地语音转写（faster-whisper）。

设计要点：
- 语音不出本机：tiny（~75MB）/ base（~145MB）CTranslate2 量化模型，纯 CPU 可跑。
- 转写结果按「文件路径 + 大小 + 修改时间」缓存到 SQLite，同一条语音只跑一次模型。
- 任何一环不可用（库没装 / 模型没下载 / 转写报错）都不让计量停摆：
  由 Meter 捕获异常后降级为「时长 × rate」。
- **模型不入库**：tiny 有 75MB 且能按 revision 从镜像站原样重新下载，
  因此只把「模型名 + 锁定版本 + 文件指纹」写进 MODEL_MANIFEST（见 docs/MODELS.md），
  新设备执行 `wequota --download-model` 即可拿到字节完全一致的一份。
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

from .config import WhisperConfig

# 一个可用的 faster-whisper 模型目录至少要有这些文件
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
_VOCAB_ALTERNATIVES = ("vocabulary.txt", "vocabulary.json")

MODEL_REPO_PREFIX = "Systran/faster-whisper-"
DEFAULT_MIRROR = "https://hf-mirror.com"

# 模型默认存放位置（相对项目根目录），随发布包附带时直接放这里
DEFAULT_MODELS_ROOT = Path(__file__).resolve().parent.parent / "assets" / "models"


@dataclass(frozen=True)
class FilePin:
    """单个模型文件的指纹：sha256 + 字节大小。"""

    sha256: str
    size: int


@dataclass(frozen=True)
class ModelPin:
    """一个可复现的模型版本：仓库名 + revision（提交号）+ 关键文件指纹。"""

    repo_id: str
    revision: str
    files: dict[str, FilePin]


# 模型清单（名称 + 版本）。模型本体不入仓库（.gitignore: assets/models/），
# 换设备时用这份清单按 revision 重新下载，拿到的就是同一份字节。
MODEL_MANIFEST: dict[str, ModelPin] = {
    "tiny": ModelPin(
        repo_id="Systran/faster-whisper-tiny",
        revision="d90ca5fe260221311c53c58e660288d3deb8d356",
        files={
            "config.json": FilePin(
                "a73a28cdfe1c43ccc7202fa333d1f89c202477271407ae9a7f19afa52039cac8", 2249),
            "model.bin": FilePin(
                "dcb76c6586fc06cbdac6dd21f14cfd129cc4cdd9dce19bf4ffa62e59cbe6e6d1", 75538270),
            "tokenizer.json": FilePin(
                "fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab", 2203239),
            "vocabulary.txt": FilePin(
                "34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913", 459861),
        },
    ),
}


def model_pin(model_size: str) -> ModelPin | None:
    """查清单里锁定的模型版本；未收录的规格返回 None（下载时不锁版本）。"""
    return MODEL_MANIFEST.get(model_size)


# faster-whisper 官方 CTranslate2 规格。清单里只收录实测过的（tiny，遗留项 C3），
# 其余规格允许下载但不保证：给出提示，别让用户以为「改个配置就一定行」。
KNOWN_MODEL_SIZES = (
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3",
)


def validate_model_size(model_size: str) -> str | None:
    """规格不合法时返回错误信息；合法返回 None。"""
    if model_size not in KNOWN_MODEL_SIZES:
        return (f"未知的语音模型规格「{model_size}」，"
                f"可选：{', '.join(KNOWN_MODEL_SIZES)}")
    return None


def model_size_note(model_size: str) -> str | None:
    """规格可用但需要注意的提示（未实测 / 未在清单里）；无需提示返回 None。"""
    error = validate_model_size(model_size)
    if error:
        return error
    if model_pin(model_size) is None:
        return f"规格 {model_size} 未在本项目实测（仅 tiny 验证过），下载与转写请自行确认"
    return None


def file_sha256(path: str | Path) -> str:
    """流式算 sha256，75MB 的 model.bin 也不会把内存吃满。"""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_model_files(model_dir: str | Path, pin: ModelPin | None) -> tuple[bool, list[str]]:
    """按清单校验目录里的模型文件；返回 (是否全部匹配, 问题列表)。"""
    if pin is None:
        return False, ["清单里没有该规格的锁定版本，无法校验"]
    base = Path(model_dir)
    problems: list[str] = []
    for name, expected in pin.files.items():
        p = base / name
        if not p.exists():
            problems.append(f"{name} 缺失")
            continue
        if p.stat().st_size != expected.size:
            problems.append(f"{name} 大小不符（期望 {expected.size}）")
            continue
        if file_sha256(p) != expected.sha256:
            problems.append(f"{name} 校验不符")
    return (not problems), problems


def cache_key(path: str | Path) -> str:
    """转写缓存键：路径 + 大小 + 修改时间（文件不存在时退化为路径）。"""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return hashlib.sha1(str(p).encode("utf-8")).hexdigest()
    raw = f"{p}|{st.st_size}|{int(st.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class TranscriptCache(Protocol):
    def get(self, key: str) -> str | None: ...

    def put(self, key: str, text: str) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, text: str) -> None:
        self._data[key] = text


class DbTranscriptCache:
    """把转写结果存进数据库，跨重启也能复用。"""

    def __init__(self, db: Any):
        self.db = db

    def get(self, key: str) -> str | None:
        return self.db.get_transcript(key)

    def put(self, key: str, text: str) -> None:
        self.db.save_transcript(key, text)


def _engine_installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


class WhisperTranscriber:
    """把语音文件转成文字。懒加载模型 + 缓存优先。"""

    def __init__(
        self,
        config: WhisperConfig | None = None,
        model_dir: str | Path | None = None,
        cache: TranscriptCache | None = None,
        loader: Callable[..., Any] | None = None,
        engine_available: bool | None = None,
    ):
        self.config = config or WhisperConfig()
        if model_dir:
            self.model_dir = Path(model_dir)
        elif self.config.model_dir:
            self.model_dir = Path(self.config.model_dir)
        else:
            self.model_dir = DEFAULT_MODELS_ROOT / f"faster-whisper-{self.config.model_size}"
        self.cache = cache or MemoryCache()
        self._loader = loader
        # 注入了 loader（测试或备用引擎）即视为引擎可用；否则按实际安装情况判断
        self._engine_available = engine_available if engine_available is not None else (
            True if loader is not None else None
        )
        self._model = None
        self._load_error: str | None = None

    # ---------- 状态 ----------

    @property
    def engine_available(self) -> bool:
        if self._engine_available is not None:
            return self._engine_available
        return _engine_installed()

    def model_ready(self) -> bool:
        """模型文件是否齐全（只看文件，不加载）。"""
        if not self.model_dir.is_dir():
            return False
        if not all((self.model_dir / f).exists() for f in MODEL_FILES if f != "vocabulary.txt"):
            return False
        return any((self.model_dir / v).exists() for v in _VOCAB_ALTERNATIVES)

    def pinned_revision(self) -> str:
        """本次下载要锁定的 revision：配置覆盖优先，否则用清单里的版本。"""
        if self.config.model_revision:
            return self.config.model_revision
        pin = model_pin(self.config.model_size)
        return pin.revision if pin else ""

    def verify_model(self) -> tuple[bool, str]:
        """按清单校验本地模型文件是否和锁定版本一致。"""
        pin = model_pin(self.config.model_size)
        ok, problems = verify_model_files(self.model_dir, pin)
        if ok:
            rev = self.pinned_revision()[:12]
            return True, f"{self.config.model_size} @{rev} 校验通过"
        return False, "；".join(problems)

    def status(self) -> dict[str, Any]:
        ready_engine = self.engine_available
        ready_model = self.model_ready()
        reason = None
        if not self.config.enabled:
            reason = "语音转写已在配置中关闭"
        elif not ready_engine:
            reason = "未安装 faster-whisper（pip install faster-whisper）"
        elif not ready_model:
            reason = f"模型 {self.config.model_size} 未下载"
        return {
            "enabled": self.config.enabled,
            "engine_installed": ready_engine,
            "model_ready": ready_model,
            "ready": bool(self.config.enabled and ready_engine and ready_model),
            "model_size": self.config.model_size,
            "model_dir": str(self.model_dir),
            "model_repo": (pin.repo_id if (pin := model_pin(self.config.model_size)) else ""),
            "model_revision": self.pinned_revision(),
            "model_note": model_size_note(self.config.model_size),
            "device": self.config.device,
            "compute_type": self.config.compute_type,
            "language": self.config.language,
            "reason": reason,
        }

    def describe(self) -> str:
        """给 CLI banner 用的一行人类可读状态。"""
        s = self.status()
        if not s["enabled"]:
            return "已关闭（config.json → whisper.enabled）"
        if not s["engine_installed"]:
            return "未安装 faster-whisper，语音按时长折算"
        if not s["model_ready"]:
            return f"模型 {s['model_size']} 未下载，语音按时长折算"
        return f"faster-whisper {s['model_size']}（{s['compute_type']}）就绪"

    # ---------- 模型 ----------

    def load(self) -> Any:
        if self._model is not None:
            return self._model
        if self._load_error:
            raise RuntimeError(self._load_error)
        if self._loader is not None:
            self._model = self._loader(
                str(self.model_dir), device=self.config.device,
                compute_type=self.config.compute_type,
            )
            return self._model
        if not self.engine_available:
            self._load_error = "faster_whisper not installed"
            raise RuntimeError(self._load_error)
        from faster_whisper import WhisperModel  # 延迟导入：没装也不影响启动

        try:
            self._model = WhisperModel(
                str(self.model_dir), device=self.config.device,
                compute_type=self.config.compute_type,
            )
        except Exception as exc:
            self._load_error = f"模型加载失败: {exc}"
            raise
        return self._model

    def download_model(self) -> tuple[bool, str]:
        """从镜像站下载模型到本地目录，返回 (是否成功, 说明)。"""
        invalid = validate_model_size(self.config.model_size)
        if invalid:                       # 别去下载一个根本不存在的 repo
            return False, invalid
        try:
            from huggingface_hub import snapshot_download
        except ImportError:
            return False, "未安装 huggingface-hub，无法自动下载模型"
        os.environ.setdefault("HF_ENDPOINT", DEFAULT_MIRROR)
        # hf-xet 传输后端在镜像站下会 401，强制走普通 HTTPS 下载
        os.environ["HF_HUB_DISABLE_XET"] = "1"
        repo = f"{MODEL_REPO_PREFIX}{self.config.model_size}"
        revision = self.pinned_revision()
        try:
            self.model_dir.mkdir(parents=True, exist_ok=True)
            kwargs: dict[str, Any] = dict(
                repo_id=repo,
                local_dir=str(self.model_dir),
                allow_patterns=["config.json", "model.bin", "tokenizer.json",
                                "vocabulary.*", "preprocessor_config.json"],
            )
            if revision:                      # 锁定版本，换设备下到的就是同一份字节
                kwargs["revision"] = revision
            snapshot_download(**kwargs)
        except Exception as exc:
            return False, f"下载失败: {exc}"
        if not self.model_ready():
            return False, "下载后文件仍不完整"
        if revision:
            ok, message = self.verify_model()
            if not ok:
                return False, f"下载内容与锁定版本 {revision[:12]} 不一致：{message}"
        return True, f"已下载 {repo}@{revision[:12]}" if revision else f"已下载 {repo}"

    def ensure_model(self, auto: bool | None = None) -> bool:
        """模型缺失时按需下载；auto 缺省用配置里的 auto_download。"""
        if self.model_ready():
            return True
        if self.config.enabled and (self.config.auto_download if auto is None else auto):
            ok, _ = self.download_model()
            return ok
        return False

    # ---------- 转写 ----------

    def transcribe(self, path: str) -> str:
        if not self.config.enabled:
            raise RuntimeError("whisper disabled")
        key = cache_key(path)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        model = self.load()
        segments, _info = model.transcribe(
            path, language=self.config.language, beam_size=self.config.beam_size,
        )
        text = "".join(_segment_text(segments)).strip()
        self.cache.put(key, text)
        return text


def _segment_text(segments: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for seg in segments:
        out.append(getattr(seg, "text", "") or "")
    return out


def build_transcriber(
    config: WhisperConfig | None = None,
    db: Any = None,
    engine_available: bool | None = None,
) -> WhisperTranscriber | None:
    """按配置构造转写器；关闭时返回 None（计量自动走时长降级）。"""
    cfg = config or WhisperConfig()
    if not cfg.enabled:
        return None
    return WhisperTranscriber(
        cfg,
        cache=DbTranscriptCache(db) if db is not None else MemoryCache(),
        engine_available=engine_available,
    )
