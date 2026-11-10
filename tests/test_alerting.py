"""M4 提醒通道测试（TDD）。

覆盖：桌面通知 / 邮件 / 大屏三通道、调度器扇出、关闭通道、冷却、免打扰时段。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from wequota.alerting import (
    AlertDispatcher,
    AlertHub,
    DashboardChannel,
    DesktopChannel,
    EmailChannel,
)
from wequota.config import AlertConfig
from wequota.quota import AlertEvent, AlertLevel

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=TZ)


def event(level: AlertLevel = AlertLevel.EXCEEDED, peer: str = "peer_a",
          used: int = 1000, limit: int = 1000) -> AlertEvent:
    return AlertEvent(
        account_id="wxid_demo", peer_id=peer, peer_name="Alice", level=level,
        used=used, limit=limit, ratio=used / limit, window_start=NOW, window_end=NOW,
    )


# ---------- 桌面通知 ----------

def test_desktop_channel_notifies():
    calls: list[tuple[str, str]] = []

    def notify(title: str, message: str) -> None:
        calls.append((title, message))

    ch = DesktopChannel(AlertConfig(), notify_fn=notify)
    assert ch.send(event()) is True
    assert len(calls) == 1
    title, message = calls[0]
    assert "WeQuota" in title
    assert "Alice" in message and "1000" in message


def test_desktop_channel_failure_returns_false():
    def boom(title: str, message: str) -> None:
        raise RuntimeError("notify-send 不存在")

    ch = DesktopChannel(AlertConfig(), notify_fn=boom)
    assert ch.send(event()) is False  # 通道失败不影响其它通道


def test_desktop_disabled_channel_skipped():
    calls: list[tuple[str, str]] = []
    ch = DesktopChannel(AlertConfig(desktop_enabled=False), notify_fn=lambda t, m: calls.append((t, m)))
    assert ch.send(event()) is False
    assert calls == []


def test_desktop_distinguishes_warn_and_exceeded():
    seen: list[str] = []
    ch = DesktopChannel(AlertConfig(), notify_fn=lambda t, m: seen.append(t))
    ch.send(event(level=AlertLevel.WARN, used=800, limit=1000))
    ch.send(event(level=AlertLevel.EXCEEDED, used=1200, limit=1000))
    assert "预警" in seen[0] and "超出" in seen[1]


# ---------- 邮件 ----------

def test_email_channel_sends_mail():
    sent: list[dict] = []

    def sender(host: str, port: int, user: str, password: str, use_tls: bool,
               mail_from: str, recipients: list[str], subject: str, body: str,
               starttls: bool = False) -> None:
        sent.append(dict(host=host, port=port, recipients=recipients,
                         subject=subject, body=body, mail_from=mail_from,
                         use_tls=use_tls, starttls=starttls))

    cfg = AlertConfig(email_enabled=True, smtp_host="smtp.qq.com", smtp_port=465,
                      smtp_user="me@qq.com", smtp_password="pwd",
                      email_to=["me@qq.com"], email_from="wequota@local")
    ch = EmailChannel(cfg, sender_fn=sender)
    assert ch.send(event()) is True
    mail = sent[0]
    assert mail["recipients"] == ["me@qq.com"]
    assert "Alice" in mail["subject"] and "1000" in mail["body"]


def test_email_channel_disabled_without_config():
    sent: list[dict] = []
    ch = EmailChannel(AlertConfig(), sender_fn=lambda **kw: sent.append(kw))
    assert ch.send(event()) is False
    assert sent == []


def test_email_failure_returns_false():
    def boom(**kw):
        raise OSError("SMTP 连接失败")

    cfg = AlertConfig(email_enabled=True, smtp_host="smtp.qq.com", email_to=["a@b.com"])
    assert EmailChannel(cfg, sender_fn=boom).send(event()) is False


# ---------- 大屏弹窗 ----------

def test_dashboard_channel_publishes():
    hub = AlertHub()
    ch = DashboardChannel(AlertConfig(), hub)
    assert ch.send(event()) is True
    recent = hub.recent()
    assert len(recent) == 1
    assert recent[0]["peer_id"] == "peer_a"
    assert recent[0]["level"] == "exceeded"
    assert "Alice" in recent[0]["message"]


def test_hub_subscribe_receives_live_events():
    hub = AlertHub()

    async def scenario():
        q = hub.subscribe()
        DashboardChannel(AlertConfig(), hub).send(event(peer="peer_b"))
        item = await asyncio.wait_for(q.get(), timeout=1)
        hub.unsubscribe(q)
        return item

    item = asyncio.run(scenario())
    assert item["peer_id"] == "peer_b"
    assert hub.subscriber_count() == 0


def test_hub_publish_from_worker_thread_wakes_waiter():
    """告警由同步线程产生时，必须能唤醒事件循环里的 WS 等待者（Windows 真机回归）。"""
    hub = AlertHub()

    async def scenario():
        q = hub.subscribe()

        def worker():
            DashboardChannel(AlertConfig(), hub).send(event(peer="peer_thread"))

        t = threading.Thread(target=worker, name="alert-worker")
        t.start()
        try:
            item = await asyncio.wait_for(q.get(), timeout=3)
        finally:
            t.join(timeout=3)
            hub.unsubscribe(q)
        return item

    item = asyncio.run(scenario())
    assert item["peer_id"] == "peer_thread"
    assert hub.subscriber_count() == 0


def test_hub_history_capped():
    hub = AlertHub(history_size=3)
    ch = DashboardChannel(AlertConfig(), hub)
    for i in range(10):
        ch.send(event(peer=f"p{i}"))
    assert len(hub.recent()) == 3
    assert hub.recent()[-1]["peer_id"] == "p9"


# ---------- 调度器 ----------

def _channels(desktop_calls: list, hub: AlertHub, mail: list | None = None):
    cfg = AlertConfig(email_enabled=bool(mail is not None), smtp_host="smtp.qq.com",
                      email_to=["a@b.com"]) if mail is not None else AlertConfig()
    desktop = DesktopChannel(cfg, notify_fn=lambda t, m: desktop_calls.append((t, m)))
    email = EmailChannel(cfg, sender_fn=lambda **kw: mail.append(kw) if mail is not None else None)
    return {"desktop": desktop, "email": email, "dashboard": DashboardChannel(AlertConfig(), hub)}


def test_dispatcher_fans_out_all_channels():
    calls: list = []
    mail: list = []
    hub = AlertHub()
    d = AlertDispatcher(_channels(calls, hub, mail), config=AlertConfig(email_enabled=True))
    result = d.dispatch([event()])
    assert result == {"desktop": True, "email": True, "dashboard": True}
    assert len(calls) == 1 and len(mail) == 1 and len(hub.recent()) == 1


def test_dispatcher_skips_disabled_channels():
    calls: list = []
    hub = AlertHub()
    channels = _channels(calls, hub)
    d = AlertDispatcher(channels, config=AlertConfig())
    result = d.dispatch([event()])
    assert result["desktop"] is True
    assert result["email"] is False      # 未配置邮件，静默跳过
    assert result["dashboard"] is True
    assert len(hub.recent()) == 1


def test_dispatcher_handles_multiple_events():
    calls: list = []
    hub = AlertHub()
    d = AlertDispatcher(_channels(calls, hub))
    result = d.dispatch([event(peer="p1"), event(peer="p2", level=AlertLevel.WARN)])
    assert len(calls) == 2 and len(hub.recent()) == 2
    assert result["desktop"] is True


def test_dispatcher_empty_events():
    calls: list = []
    hub = AlertHub()
    d = AlertDispatcher(_channels(calls, hub))
    assert d.dispatch([]) == {}
    assert calls == [] and hub.recent() == []


def test_cooldown_suppresses_repeat():
    calls: list = []
    hub = AlertHub()
    cfg = AlertConfig(cooldown_seconds=300)
    d = AlertDispatcher(_channels(calls, hub), config=cfg, now_fn=lambda: NOW)

    assert d.dispatch([event()])["desktop"] is True
    # 5 分钟内同一联系人同档位不重复打扰
    later = d.dispatch([event()], now=NOW + timedelta(seconds=60))
    assert later == {}
    assert len(calls) == 1
    # 超过冷却期后可再次提醒
    assert d.dispatch([event()], now=NOW + timedelta(seconds=301))["desktop"] is True


def test_cooldown_is_per_peer_and_level():
    calls: list = []
    hub = AlertHub()
    d = AlertDispatcher(_channels(calls, hub), config=AlertConfig(cooldown_seconds=300),
                        now_fn=lambda: NOW)
    d.dispatch([event(peer="a")])
    assert d.dispatch([event(peer="b")])["desktop"] is True              # 不同联系人
    assert d.dispatch([event(peer="a", level=AlertLevel.WARN)]) != {}     # 不同档位


def test_quiet_hours_suppress_all_channels():
    calls: list = []
    hub = AlertHub()
    cfg = AlertConfig(quiet_hours=[23, 7])
    night = datetime(2026, 10, 12, 23, 30, tzinfo=TZ)
    d = AlertDispatcher(_channels(calls, hub), config=cfg, now_fn=lambda: night)
    assert d.dispatch([event()]) == {}
    assert calls == [] and hub.recent() == []
    # 白天正常
    day = AlertDispatcher(_channels(calls, hub), config=cfg, now_fn=lambda: NOW)
    assert day.dispatch([event()])["desktop"] is True


def test_dispatcher_channel_status():
    calls: list = []
    hub = AlertHub()
    d = AlertDispatcher(_channels(calls, hub), config=AlertConfig())
    status = d.channel_status()
    assert status["desktop"] == "enabled"
    assert status["email"] == "未配置 SMTP"
    assert status["dashboard"] == "enabled"


# ---------------- M8+：平台通知命令构造（Windows/macOS 真机前先静态验证） ----------------

def test_windows_notify_command_escapes_single_quotes():
    from wequota.alerting import build_notify_command
    cmd, _ = build_notify_command("Windows", "It's ok", "Bob's 90%")
    ps = cmd[-1]
    assert "''" in ps                     # 单引号翻倍是 PowerShell 的转义方式
    assert cmd[0] == "powershell" and "-NoProfile" in cmd


def test_notify_command_strips_newlines_and_caps_length():
    from wequota.alerting import build_notify_command
    long_msg = "第一行\n第二行" + "字" * 500
    cmd, _ = build_notify_command("Windows", "标题", long_msg)
    ps = cmd[-1]
    assert "\n" not in ps
    assert len(ps) < 800                  # 不能把超长文本塞进命令行


def test_macos_notify_command_escapes_quotes():
    from wequota.alerting import build_notify_command
    cmd, timeout = build_notify_command("Darwin", "标题", '他说"你好"')
    assert cmd[0] == "osascript" and '\\"' in cmd[-1]
    assert timeout == 10


def test_linux_notify_command():
    from wequota.alerting import build_notify_command
    cmd, _ = build_notify_command("Linux", "t", "m")
    assert cmd == ["notify-send", "t", "m"]


def test_system_notify_falls_back_to_pwsh_on_windows():
    from wequota import alerting
    calls = []

    def runner(cmd, timeout):
        calls.append(cmd[0])
        if cmd[0] == "powershell":
            raise FileNotFoundError("powershell")
        return None

    alerting._system_notify("t", "m", system="Windows", runner=runner)
    assert calls == ["powershell", "pwsh"]


def test_system_notify_does_not_fallback_on_macos():
    from wequota import alerting

    def runner(cmd, timeout):
        raise FileNotFoundError("osascript")

    try:
        alerting._system_notify("t", "m", system="Darwin", runner=runner)
    except FileNotFoundError:
        pass                              # macOS 没有备选解释器，直接抛出由通道吞掉
    else:
        raise AssertionError("macOS 不应回退到别的命令")


# ---------- 加密方式（M11：465 SSL / 587 STARTTLS） ----------

def _mail_kwargs():
    sent: list[dict] = []

    def sender(**kw):
        sent.append(kw)

    return sent, sender


def test_ssl_security_uses_implicit_ssl():
    sent, sender = _mail_kwargs()
    cfg = AlertConfig(email_enabled=True, smtp_host="smtp.qq.com", smtp_port=465,
                      smtp_security="ssl", smtp_user="me@qq.com", email_to=["me@qq.com"])
    assert EmailChannel(cfg, sender_fn=sender).send(event()) is True
    assert sent[0]["use_tls"] is True and sent[0]["starttls"] is False


def test_starttls_security_upgrades_after_hello():
    sent, sender = _mail_kwargs()
    cfg = AlertConfig(email_enabled=True, smtp_host="smtp.gmail.com", smtp_port=587,
                      smtp_security="starttls", smtp_user="me@gmail.com",
                      email_to=["me@gmail.com"])
    assert EmailChannel(cfg, sender_fn=sender).send(event()) is True
    assert sent[0]["use_tls"] is False and sent[0]["starttls"] is True


def test_none_security_sends_plain():
    sent, sender = _mail_kwargs()
    cfg = AlertConfig(email_enabled=True, smtp_host="mail.local", smtp_port=25,
                      smtp_security="none", smtp_user="a@b.com", email_to=["a@b.com"])
    assert EmailChannel(cfg, sender_fn=sender).send(event()) is True
    assert sent[0]["use_tls"] is False and sent[0]["starttls"] is False


def test_legacy_use_tls_false_maps_to_plain():
    """老配置 `smtp_use_tls: false` 应等价于 none，而不是继续走 SSL。"""
    cfg = AlertConfig.from_dict({"smtp_use_tls": False})
    assert cfg.smtp_security == "none"
    assert cfg.smtp_tls_mode == (False, False)


def test_legacy_use_tls_true_maps_to_ssl():
    cfg = AlertConfig.from_dict({"smtp_use_tls": True})
    assert cfg.smtp_security == "ssl"
    assert cfg.smtp_tls_mode == (True, False)


def test_unknown_security_falls_back_to_ssl():
    assert AlertConfig(smtp_security="wat").smtp_tls_mode == (True, False)


def test_smtp_failure_exposes_readable_reason():
    def boom(**kw):
        raise OSError("Connection refused")

    cfg = AlertConfig(email_enabled=True, smtp_host="smtp.qq.com", email_to=["a@b.com"])
    ch = EmailChannel(cfg, sender_fn=boom)
    assert ch.send(event()) is False
    assert ch.last_error and "连不上" in ch.last_error
