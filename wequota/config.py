"""全局配置：计量系数等可调参数。

所有 Token 折算系数集中在此，用户可通过 config.json 覆盖（见 load_config）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any


@dataclass
class MeteringConfig:
    """Token 计量系数。"""

    # 文字：LLM tokenizer 口径
    text_encoding: str = "cl100k_base"

    # 图片 / 表情包：多模态模型 patch 口径
    image_tile_size: int = 512
    tokens_per_tile: int = 170
    image_base_tokens: int = 85
    default_image_width: int = 1024
    default_image_height: int = 768

    # 视频消息：抽帧口径
    video_fps: float = 1.0
    video_max_frames: int = 32
    tokens_per_video_frame: int = 255
    default_video_frames: int = 16

    # 通话：时长驱动，双方同时输出故 ×2
    voice_call_rate: float = 25.0      # token / 秒 / 方向
    video_call_rate: float = 255.0
    call_duplex_factor: int = 2

    # 语音消息降级（无转写模型 / 转写失败）：按时长固定折算
    # 注：微信语音单条最长 60 秒，不做时长上限，来了就转写
    voice_fallback_rate: float = 25.0

    @classmethod
    def from_dict(cls, data: dict) -> "MeteringConfig":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass
class AlertConfig:
    """提醒通道配置（M4）。"""

    # 桌面系统通知
    desktop_enabled: bool = True

    # 邮件（SMTP）
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    # 传输加密方式：ssl（端口 465，隐式 SSL）/ starttls（端口 587，明文握手后升级）/ none（端口 25，不加密）
    smtp_security: str = "ssl"
    # 兼容旧字段：仅当配置文件里没有 smtp_security 时才由它推断（True→ssl / False→none）
    smtp_use_tls: bool = True
    email_from: str = ""
    email_to: list[str] = field(default_factory=list)

    # 大屏弹窗（WebSocket 推送）
    dashboard_enabled: bool = True

    # 冷却：同一联系人同一档位在该秒数内不重复打扰
    cooldown_seconds: int = 300
    # 免打扰时段 [起始小时, 结束小时]，None 表示不启用；跨零点如 [23, 7]
    quiet_hours: list[int] | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "AlertConfig":
        valid = {f.name for f in fields(cls)}
        payload = {k: v for k, v in data.items() if k in valid}
        # 老配置只有 smtp_use_tls：True→ssl、False→none；新配置以 smtp_security 为准
        if "smtp_security" not in data and "smtp_use_tls" in data:
            payload["smtp_security"] = "ssl" if data["smtp_use_tls"] else "none"
        return cls(**payload)

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.email_to)

    @property
    def smtp_tls_mode(self) -> tuple[bool, bool]:
        """返回 (use_tls, starttls)，供 smtplib 选择连接方式；非法值按 ssl 处理。"""
        mode = str(self.smtp_security or "ssl").strip().lower()
        if mode == "starttls":
            return False, True
        if mode == "none":
            return False, False
        return True, False


@dataclass
class WhisperConfig:
    """语音转写配置（M6）：本地 faster-whisper，纯 CPU 也能跑。"""

    enabled: bool = True
    # tiny ~75MB / base ~145MB；弱性能机器保持 tiny
    model_size: str = "tiny"
    # 模型目录，留空则默认 assets/models/faster-whisper-<size>
    model_dir: str = ""
    # 锁定版本（HF revision）。留空用 transcribe.MODEL_MANIFEST 里登记的版本，
    # 保证换设备重新下载时拿到字节完全一致的一份；模型本身不入库
    model_revision: str = ""
    device: str = "auto"            # auto / cpu
    compute_type: str = "int8"      # int8 量化，CPU 友好
    language: str = "zh"
    beam_size: int = 1              # 1 = 贪心解码，最省 CPU
    # 模型缺失时是否自动从镜像站下载（首次启动后台进行）
    auto_download: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "WhisperConfig":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass
class QuotaConfig:
    """限额默认配置（M10）：未单独设规则的联系人按默认额度计。"""

    # 新联系人/未单独设置时的默认日额度（token）；0 = 不设默认（未设额度的联系人不提醒）
    default_limit_tokens: int = 50000

    @classmethod
    def from_dict(cls, data: dict) -> "QuotaConfig":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass
class AppConfig:
    """应用自身开关（M10 收尾）：演示入口与数据库快照。"""

    # /api/ingest/mock 演示数据入口。接入真实采集后可关掉，避免误触污染真实统计
    allow_mock_ingest: bool = True
    # 大屏/ API 访问令牌；留空 = 不启用鉴权。启用后 API 需 Bearer，WS 需 ?token=
    auth_token: str = ""
    # 允许跨域访问的来源列表；留空 = 仅同源（默认最安全）。支持显式 "*"
    cors_origins: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "AppConfig":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass
class BackupConfig:
    """数据库快照备份（遗留项 F1 的轻量版）：每天一份，保留最近 N 份。"""

    enabled: bool = True
    # 一天最多一份（按文件名里的日期判断），重复启动不会刷屏
    keep: int = 3
    # 快照存放目录，留空 = 数据目录下 backups/
    dir: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "BackupConfig":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


@dataclass
class CollectorConfig:
    """采集器配置（M7）：wechat-cli 适配器 + 同步心跳。"""

    # 关闭后不启动采集（纯大屏查看 / 只用备份导入器）
    enabled: bool = True
    # 当前微信号标识（多账号数据隔离的键）
    account_id: str = "wxid_demo"
    # wechat-cli 可执行文件路径，留空则用 PATH 里的 wechat-cli
    binary: str = ""
    # 首次启动缺失时自动安装（Windows pip / macOS npm 优先、pip 回退）
    auto_install: bool = True
    # 自动安装时锁定的 wechat-cli 版本（npm / --version 比对）；留空 = PINNED_CLI_VERSION
    cli_version: str = ""
    # git 安装锁定的 commit/tag/branch；留空 = PINNED_CLI_COMMIT，"latest" = 跟踪默认分支
    cli_ref: str = ""
    # 心跳轮询间隔（秒）；每轮会执行 contacts + history 两次 CLI 调用
    poll_interval_s: float = 15.0
    # 微信未登录/采集失败进入休眠后的轮询间隔（秒）
    sleep_interval_s: float = 60.0
    # 重新登录后回退游标补读的天数（覆盖手机端聊天）
    catchup_days: int = 3
    # 连续失败多少次进入休眠
    max_failures: int = 3
    # 单次轮询最多拉取多少条
    limit_per_poll: int = 500
    # 启动后自动开始轮询
    auto_start: bool = True
    # 是否采集群聊（v0.1 默认仅私聊）
    include_groups: bool = False
    # 首次接入是否全量回填历史；之后一律按 create_time 增量
    full_backfill: bool = True
    # 全量回填期间单轮每联系人最多拉取多少条；0 = 一次拉完。
    # 回填未追平时保持该上限，追平后转回 `limit_per_poll`。
    full_backfill_limit: int = 5000
    # huohuoer 解密副本缓存目录，留空 = 系统临时目录下的 wechat_cli_cache
    cache_dir: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "CollectorConfig":
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in valid})


def load_config(path: str | Path = "config.json") -> MeteringConfig:
    """从配置文件加载，缺失则全部用默认值。"""
    p = Path(path)
    if not p.exists():
        return MeteringConfig()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return MeteringConfig()
    return MeteringConfig.from_dict(data.get("metering", {}))


def _read_config_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_alert_config(path: str | Path = "config.json") -> AlertConfig:
    """读取 config.json 的 alert 段，缺失则用默认值。"""
    return AlertConfig.from_dict(_read_config_file(path).get("alert", {}))


def load_whisper_config(path: str | Path = "config.json") -> WhisperConfig:
    """读取 config.json 的 whisper 段，缺失则用默认值。"""
    return WhisperConfig.from_dict(_read_config_file(path).get("whisper", {}))


def load_quota_config(path: str | Path = "config.json") -> QuotaConfig:
    """读取 config.json 的 quota 段，缺失则用默认值。"""
    return QuotaConfig.from_dict(_read_config_file(path).get("quota", {}))


def load_collector_config(path: str | Path = "config.json") -> CollectorConfig:
    """读取 config.json 的 collector 段，缺失则用默认值。"""
    return CollectorConfig.from_dict(_read_config_file(path).get("collector", {}))


def load_app_config(path: str | Path = "config.json") -> AppConfig:
    """读取 config.json 的 app 段，缺失则用默认值。"""
    return AppConfig.from_dict(_read_config_file(path).get("app", {}))


def load_backup_config(path: str | Path = "config.json") -> BackupConfig:
    """读取 config.json 的 backup 段，缺失则用默认值。"""
    return BackupConfig.from_dict(_read_config_file(path).get("backup", {}))


CONFIG_SCHEMA_VERSION = 1


def dump_default_config(path: str | Path = "config.json") -> None:
    """生成默认配置文件，方便用户调整系数与提醒设置。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"schema_version": CONFIG_SCHEMA_VERSION,
                    "metering": asdict(MeteringConfig()), "alert": asdict(AlertConfig()),
                    "whisper": asdict(WhisperConfig()),
                    "quota": asdict(QuotaConfig()),
                    "app": asdict(AppConfig()), "backup": asdict(BackupConfig()),
                    "collector": asdict(CollectorConfig())},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def read_config_version(path: str | Path = "config.json") -> int:
    """读配置文件的 schema 版本；文件不存在/没写/不可解析都算 0（= 未标注版本）。"""
    data = _read_config_file(path)
    value = data.get("schema_version", 0)
    return value if isinstance(value, int) else 0


def config_version_note(path: str | Path = "config.json") -> str | None:
    """版本对不上时的提示；一致则返回 None。

    - 0（老配置）：缺的字段已由默认值兜底，提示用户跑一次 `--setup` 重新落盘
    - 高于当前：配置文件来自更高版本，可能有本版不认识的字段
    """
    found = read_config_version(path)
    if found == CONFIG_SCHEMA_VERSION:
        return None
    if found == 0:
        return ("配置文件未标注版本（老格式），缺失字段已按默认值处理；"
                "可执行 `wequota --setup` 重新保存为标准格式")
    if found > CONFIG_SCHEMA_VERSION:
        return (f"配置文件版本 {found} 高于本程序支持的 {CONFIG_SCHEMA_VERSION}，"
                "其中较新的字段会被忽略")
    return (f"配置文件版本 {found} 低于当前的 {CONFIG_SCHEMA_VERSION}，"
            "缺失字段已按默认值处理")
