"""M4 提醒通道：把限额引擎产生的 AlertEvent 投递到桌面通知 / 邮件 / 数据大屏。

设计要点：
- 三通道彼此独立，任一通道失败不影响其它通道，也不影响主流程（永远不抛异常到调用方）。
- 桌面通知优先用系统原生方式，不强制依赖第三方包；失败静默降级。
- 邮件未配置 SMTP 时整条通道跳过，不打扰用户。
- 大屏弹窗走进程内 AlertHub（WebSocket 推送 + 历史回放），前端连上即收到最近告警。
- 冷却与免打扰时段在调度器层统一处理，避免反复改额导致提醒轰炸。
"""

from __future__ import annotations

import asyncio
import html as html_lib
import platform
import smtplib
import subprocess
from collections import deque
from datetime import datetime
from email.header import Header
from email.mime.text import MIMEText
from typing import Any, Callable, Iterable, Protocol

from .config import AlertConfig
from .quota import AlertEvent, AlertLevel, money_alert_event

LEVEL_LABEL = {
    AlertLevel.WARN: "额度预警",
    AlertLevel.EXCEEDED: "额度超出",
}

MONEY_LABEL = {"transfer": "转账提醒", "red_packet": "红包提醒"}


def render_title(event: AlertEvent) -> str:
    if event.is_money_event():
        return f"WeQuota · {MONEY_LABEL.get(event.kind or '', '资金提醒')}"
    return f"WeQuota · {LEVEL_LABEL.get(event.level, '额度提醒')}"


def render_subject(event: AlertEvent) -> str:
    who = event.peer_name or event.peer_id
    if event.is_money_event():
        return f"[WeQuota] {MONEY_LABEL.get(event.kind or '', '资金提醒')}：{who}"
    return f"[WeQuota] {LEVEL_LABEL.get(event.level, '额度提醒')}：{who}"


def render_body(event: AlertEvent) -> str:
    """纯文本正文（桌面通知、Web 弹窗与无 HTML 能力时使用）。"""
    if event.is_money_event():
        label = "转账" if event.kind == "transfer" else "红包"
        lines = [event.message()]
        if event.amount_yuan is None:
            lines.append("金额需在微信内打开查看。")
        lines.append(f"联系人 ID：{event.peer_id}")
        lines.append("")
        lines.append("— 本邮件由本地运行的 WeQuota 发送，数据未离开你的电脑。")
        return "\n".join(lines)
    lines = [event.message()]
    if event.window_start:
        lines.append(f"统计窗口：{event.window_start:%Y-%m-%d %H:%M} 起")
    lines.append(f"联系人 ID：{event.peer_id}")
    lines.append("")
    lines.append("— 本邮件由本地运行的 WeQuota 发送，数据未离开你的电脑。")
    return "\n".join(lines)


def render_html_body(event: AlertEvent) -> str | None:
    """资金事件的 HTML 正文：金额醒目加粗放大；其它事件返回 None（走纯文本）。"""
    if not event.is_money_event():
        return None
    who = html_lib.escape(event.peer_name or event.peer_id)
    label = "转账" if event.kind == "transfer" else "红包"
    if event.amount_yuan is not None:
        amount = html_lib.escape(f"¥{event.amount_yuan:.2f}")
        highlight = (
            f'<p style="margin:8px 0;font-size:15px;color:#333">'
            f'{who} 给你发来一笔{label}：</p>'
            f'<p style="margin:8px 0;font-size:28px;font-weight:700;color:#c0392b">{amount}</p>'
        )
    else:
        highlight = (
            f'<p style="margin:8px 0;font-size:15px;color:#333">'
            f'{who} 给你发来一个{label}，'
            f'<b style="color:#c0392b">金额需在微信内查看</b>。</p>'
        )
    return (
        '<html><body style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
        f'<h2 style="color:#0b7285;margin:0 0 12px">WeQuota · {label}提醒</h2>'
        f'{highlight}'
        f'<p style="color:#888;font-size:12px">联系人 ID：{html_lib.escape(event.peer_id)}</p>'
        '<hr style="border:none;border-top:1px solid #eee">'
        '<p style="color:#888;font-size:12px">— 本邮件由本地运行的 WeQuota 发送，数据未离开你的电脑。</p>'
        '</body></html>'
    )


# ---------------------------------------------------------------- 桌面通知


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _sanitize(text: str, limit: int = 160) -> str:
    """通知文案压成单行并限长：换行会截断命令行/脚本，超长会撑爆参数。"""
    return " ".join(str(text).split())[:limit]


def build_notify_command(system: str, title: str, message: str) -> tuple[list[str], int]:
    """构造平台原生通知命令（返回 命令 + 超时秒）。纯函数，便于跨平台静态验证。"""
    title, message = _sanitize(title), _sanitize(message, limit=220)
    if system == "Darwin":
        script = f'display notification "{_esc(message)}" with title "{_esc(title)}"'
        return ["osascript", "-e", script], 10
    if system == "Windows":
        ps = (
            "[reflection.assembly]::LoadWithPartialName('System.Windows.Forms')|Out-Null;"
            "$n=New-Object System.Windows.Forms.NotifyIcon;"
            "$n.Icon=[System.Drawing.SystemIcons]::Information;"
            "$n.BalloonTipTitle='%s';$n.BalloonTipText='%s';$n.Visible=$true;"
            "$n.ShowBalloonTip(8000);Start-Sleep -Seconds 6;$n.Dispose()"
            % (title.replace("'", "''"), message.replace("'", "''"))
        )
        return ["powershell", "-NoProfile", "-Command", ps], 15
    return ["notify-send", title, message], 10


def _system_notify(title: str, message: str, system: str | None = None,
                   runner: Callable[[list[str], int], Any] | None = None) -> None:
    """调用系统原生通知能力，全部平台都不依赖额外 pip 包。"""
    system = system or platform.system()

    def _default(cmd: list[str], timeout: int) -> None:
        subprocess.run(cmd, check=True, timeout=timeout, capture_output=True)

    run = runner or _default
    cmd, timeout = build_notify_command(system, title, message)
    try:
        run(cmd, timeout)
    except FileNotFoundError:
        # Windows 上精简版系统可能只有 pwsh（PowerShell 7）
        if system == "Windows":
            alt, _ = build_notify_command(system, title, message)
            alt[0] = "pwsh"
            run(alt, timeout)
        else:
            raise


# ---------------------------------------------------------------- 通道定义


class AlertChannel(Protocol):
    name: str

    def send(self, event: AlertEvent) -> bool: ...

    def status(self) -> str: ...


class DesktopChannel:
    """PC 桌面系统通知（macOS 通知中心 / Windows 气泡 / Linux notify-send）。"""

    name = "desktop"

    def __init__(self, config: AlertConfig,
                 notify_fn: Callable[[str, str], None] | None = None) -> None:
        self.config = config
        self._notify = notify_fn or _system_notify

    def send(self, event: AlertEvent) -> bool:
        if not self.config.desktop_enabled:
            return False
        try:
            self._notify(render_title(event), event.message())
        except Exception:
            return False
        return True

    def status(self) -> str:
        if not self.config.desktop_enabled:
            return "已关闭"
        return "enabled"


def _smtp_reason(exc: BaseException) -> str:
    """把 SMTP 异常翻成人能看懂的话——配置页里最常见的就是授权码和端口填错。"""
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if isinstance(exc, smtplib.SMTPAuthenticationError) or "535" in text \
            or "authentication" in low:
        return f"认证失败（{text}）：QQ / 网易邮箱要填 16 位「授权码」，不是登录密码"
    if isinstance(exc, smtplib.SMTPConnectError) or "getaddrinfo" in low \
            or "name or service not known" in low or "nodename nor servname" in low \
            or "refused" in low:
        return f"连不上服务器（{text}）：检查 SMTP 地址、端口与网络"
    if isinstance(exc, smtplib.SMTPServerDisconnected) or "disconnected" in low:
        return f"服务器主动断开（{text}）：多半是端口与加密方式不配套"
    if "ssl" in low or "tls" in low or "wrong version number" in low:
        return f"加密握手失败（{text}）：检查端口与加密方式是否配套"
    if "timed out" in low or "timeout" in low:
        return f"连接超时（{text}）"
    return text


class EmailChannel:
    """SMTP 邮件提醒。未配置 SMTP 时整条通道静默跳过。"""

    name = "email"

    def __init__(self, config: AlertConfig,
                 sender_fn: Callable[..., None] | None = None) -> None:
        self.config = config
        self._sender = sender_fn or self._smtp_send
        # 最近一次发送失败的原因（供大屏「发测试邮件」展示，日常通道仍静默降级）
        self.last_error: str | None = None

    def send(self, event: AlertEvent) -> bool:
        if not self.config.email_enabled or not self.config.email_configured:
            self.last_error = ("邮件提醒未启用" if not self.config.email_enabled
                               else "SMTP 服务器或收件人未配置")
            return False
        mail_from = self.config.email_from or self.config.smtp_user or "wequota@localhost"
        subject = render_subject(event)
        try:
            use_tls, starttls = self.config.smtp_tls_mode
            self._sender(
                host=self.config.smtp_host, port=self.config.smtp_port,
                user=self.config.smtp_user, password=self.config.smtp_password,
                use_tls=use_tls, starttls=starttls, mail_from=mail_from,
                recipients=list(self.config.email_to),
                subject=subject, body=render_html_body(event) or render_body(event),
            )
        except Exception as exc:
            self.last_error = _smtp_reason(exc)
            return False
        self.last_error = None
        return True

    def _smtp_send(self, host: str, port: int, user: str, password: str, use_tls: bool,
                   mail_from: str, recipients: list[str], subject: str, body: str,
                   starttls: bool = False) -> None:
        # 正文以 "<" 开头视为 HTML（资金提醒用 HTML 加粗金额），否则纯文本
        is_html = body.lstrip().startswith("<")
        msg = MIMEText(body, "html" if is_html else "plain", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = mail_from
        msg["To"] = ", ".join(recipients)
        if use_tls:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if starttls:                       # 587：明文握手后升级为 TLS
                server.starttls()
        try:
            if user:
                server.login(user, password)
            server.sendmail(mail_from, recipients, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

    def status(self) -> str:
        if not self.config.email_configured:
            return "未配置 SMTP"
        if not self.config.email_enabled:
            return "已关闭"
        return "enabled"


class AlertHub:
    """进程内告警总线：WebSocket 订阅 + 最近告警历史（新连接可回放）。

    告警可能由**同步线程**（FastAPI 同步接口的线程池、SyncWorker）产生，而
    `asyncio.Queue` 不是线程安全的：在别的线程里 `put_nowait` 不会唤醒事件循环里
    等待的 `await queue.get()`，大屏会偶发收不到实时告警（Windows 真机实测可稳定复现）。
    因此订阅时记录所属事件循环，发布时统一走 `loop.call_soon_threadsafe`。
    """

    def __init__(self, history_size: int = 50) -> None:
        self._history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers[q] = asyncio.get_running_loop()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.pop(q, None)

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def recent(self) -> list[dict[str, Any]]:
        return list(self._history)

    def publish(self, payload: dict[str, Any]) -> None:
        self._history.append(payload)
        for q, loop in list(self._subscribers.items()):
            if loop.is_closed():
                self._subscribers.pop(q, None)
                continue
            try:
                loop.call_soon_threadsafe(q.put_nowait, payload)
            except RuntimeError:                  # 循环正在关闭
                self._subscribers.pop(q, None)


class DashboardChannel:
    """数据大屏弹窗：推入 AlertHub，由 /ws/alerts 推给前端。"""

    name = "dashboard"

    def __init__(self, config: AlertConfig, hub: AlertHub) -> None:
        self.config = config
        self.hub = hub

    def send(self, event: AlertEvent) -> bool:
        if not self.config.dashboard_enabled:
            return False
        payload = event.model_dump(mode="json")
        payload["message"] = event.message()
        payload["title"] = render_title(event)
        payload["created_at"] = datetime.now().astimezone().isoformat()
        self.hub.publish(payload)
        return True

    def status(self) -> str:
        if not self.config.dashboard_enabled:
            return "已关闭"
        return "enabled"


# ---------------------------------------------------------------- 调度器


class AlertDispatcher:
    """把告警事件扇出到各通道，并处理冷却与免打扰。"""

    def __init__(
        self,
        channels: dict[str, AlertChannel],
        config: AlertConfig | None = None,
        db: Any | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self.channels = channels
        self.config = config or AlertConfig()
        self.db = db
        self._now = now_fn or (lambda: datetime.now().astimezone())
        self._last_sent: dict[tuple[str, str, str], datetime] = {}

    # ---------- 主入口 ----------

    def dispatch(self, events: Iterable[AlertEvent],
                 now: datetime | None = None) -> dict[str, bool]:
        now = now or self._now()
        events = list(events)
        if not events:
            return {}
        if self._in_quiet_hours(now):
            return {}
        pending = [e for e in events if not self._throttled(e, now)]
        if not pending:
            return {}

        result: dict[str, bool] = {}
        for name, channel in self.channels.items():
            ok = False
            for e in pending:
                # 注意：不能用 any(生成器)，那会在第一条成功后短路，漏掉后面的事件
                if channel.send(e):
                    ok = True
            result[name] = ok
        for e in pending:
            self._last_sent[(e.peer_id, e.level.value, e.kind or "")] = now
            if self.db is not None and e.alert_id is not None:
                try:
                    self.db.update_alert_channels(e.alert_id, result)
                except Exception:
                    pass
        return result

    def channel_status(self) -> dict[str, str]:
        return {name: ch.status() for name, ch in self.channels.items()}

    # ---------- 抑制规则 ----------

    def _throttled(self, event: AlertEvent, now: datetime) -> bool:
        key = (event.peer_id, event.level.value, event.kind or "")
        last = self._last_sent.get(key)
        if last is None:
            return False
        return (now - last).total_seconds() < max(self.config.cooldown_seconds, 0)

    def _in_quiet_hours(self, now: datetime) -> bool:
        hours = self.config.quiet_hours
        if not hours or len(hours) != 2:
            return False
        start, end = int(hours[0]), int(hours[1])
        h = now.astimezone().hour
        if start == end:
            return False
        if start < end:
            return start <= h < end
        return h >= start or h < end


def build_dispatcher(
    config: AlertConfig | None = None,
    db: Any | None = None,
    hub: AlertHub | None = None,
) -> tuple[AlertDispatcher, AlertHub]:
    """按配置组装默认三通道调度器（供 app / cli 复用）。"""
    config = config or AlertConfig()
    hub = hub or AlertHub()
    return AlertDispatcher(
        {
            "desktop": DesktopChannel(config),
            "email": EmailChannel(config),
            "dashboard": DashboardChannel(config, hub),
        },
        config=config, db=db,
    ), hub


def dispatch_money_events(dispatcher: AlertDispatcher, db: Any, account_id: str,
                          limit: int = 500) -> int:
    """把未处理的转账/红包事件派发出去；仅对设有额度规则的联系人提醒。

    返回实际派发的事件数。数据库缺少相关表/方法时静默跳过。
    """
    try:
        # 首次调用时设置跟踪起点；60s grace 只影响「直接派发」场景，
        # 正常轮询会在 ingest 前先调用一次 ensure_money_alert_since()。
        cutoff = db.ensure_money_alert_since(account_id, grace_seconds=60)
        db.mark_money_alerted_before(account_id, cutoff)
        rows = db.pending_money_messages(
            account_id=account_id, since=cutoff, limit=limit)
    except (AttributeError, TypeError):
        return 0
    sent = 0
    for row in rows:
        message_id = row.get("id")
        peer_id = row.get("peer_id", "")
        if db.get_rule(account_id, peer_id) is None:
            # 未设额度规则的联系人不提醒；标记已处理，避免后续反复扫描
            if message_id is not None:
                db.mark_money_alerted(message_id)
            continue
        extra = row.get("extra") or {}
        event = money_alert_event(
            account_id=account_id, peer_id=peer_id,
            peer_name=row.get("peer_name", ""),
            kind=extra.get("kind") or row.get("msg_type"),
            amount_yuan=extra.get("amount_yuan"),
        )
        delivered = dispatcher.dispatch([event])
        if not any(delivered.values()):
            # 免打扰 / 冷却中 / 无可用通道：不标记，留待下一轮重试
            continue
        sent += 1
        if message_id is not None:
            try:
                db.mark_money_alerted(message_id)
            except Exception:
                pass
    return sent
