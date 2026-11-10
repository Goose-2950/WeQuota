"""M13 转账 / 红包解析测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wequota.alerting import AlertDispatcher, dispatch_money_events
from wequota.config import AlertConfig
from wequota.contracts import MessageEnvelope
from wequota.money import (
    KIND_RED_PACKET,
    KIND_TRANSFER,
    parse_money,
)
from wequota.storage import Database

TRANSFER_XML = (
    '<msg><appmsg><type>2000</type><title>转账</title>'
    '<wcpayinfo><feedesc>¥100.00</feedesc><pay_memo>还款</pay_memo></wcpayinfo>'
    '</appmsg></msg>'
)
RED_PACKET_XML = (
    '<msg><appmsg><type>2001</type>'
    '<wcpayinfo><sendertitle>恭喜发财</sendertitle></wcpayinfo>'
    '</appmsg></msg>'
)


def test_parse_transfer_extracts_amount_and_memo():
    result = parse_money(TRANSFER_XML, 49)
    assert result is not None
    assert result["kind"] == KIND_TRANSFER
    assert result["amount_yuan"] == 100.0
    assert result["memo"] == "还款"


def test_parse_red_packet_has_no_amount():
    result = parse_money(RED_PACKET_XML, 49)
    assert result is not None
    assert result["kind"] == KIND_RED_PACKET
    assert result["amount_yuan"] is None
    assert result["title"] == "恭喜发财"


def test_parse_ignores_non_appmsg():
    assert parse_money("你好，在吗", 1) is None
    assert parse_money("<msg><appmsg><type>5</type></appmsg></msg>", 49) is None


def test_parse_rejects_xxe_and_oversized_payloads():
    xxe = "<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]>" + TRANSFER_XML
    assert parse_money(xxe, 49) is None
    assert parse_money(TRANSFER_XML + "A" * 30000, 49) is None


def test_parse_handles_currency_and_missing_feedesc():
    result = parse_money(
        '<msg><appmsg><type>2000</type><wcpayinfo>'
        '<feedesc>￥ 88</feedesc></wcpayinfo></appmsg></msg>', 49)
    assert result is not None and result["amount_yuan"] == 88.0
    # 转账但金额字段缺失 → kind 仍是 transfer，amount 为 None
    no_amount = parse_money(
        '<msg><appmsg><type>2000</type><title>转账</title></appmsg></msg>', 49)
    assert no_amount is not None
    assert no_amount["kind"] == KIND_TRANSFER and no_amount["amount_yuan"] is None


def _money_env(peer: str, msg_id: str, kind: str, amount: float | None,
               account_id: str = "wxid_me",
               timestamp: datetime | None = None) -> MessageEnvelope:
    return MessageEnvelope(
        account_id=account_id, msg_id=msg_id, peer_id=peer, peer_name=peer,
        direction="recv", msg_type=kind,
        timestamp=timestamp or datetime.now(timezone.utc),
        extra={"kind": kind, "amount_yuan": amount},
    )


class _Capture:
    name = "capture"

    def __init__(self) -> None:
        self.events: list = []

    def send(self, event) -> bool:
        self.events.append(event)
        return True


def test_dispatch_money_events_only_for_peers_with_rules(tmp_path):
    db = Database(tmp_path / "m.db")
    db.init()
    db.upsert_messages([
        _money_env("p_rule", "m1", "transfer", 10.0),
        _money_env("p_norule", "m2", "red_packet", None),
    ])
    db.upsert_rule({"account_id": "wxid_me", "peer_id": "p_rule",
                    "window_type": "daily", "limit_tokens": 100})
    cap = _Capture()
    dispatcher = AlertDispatcher({"capture": cap}, config=AlertConfig())

    sent = dispatch_money_events(dispatcher, db, "wxid_me")
    assert sent == 1
    assert cap.events[0].peer_id == "p_rule"
    assert cap.events[0].amount_yuan == 10.0 and cap.events[0].kind == "transfer"
    # 两条消息都被标记，不会重复提醒
    assert db.pending_money_messages() == []


def test_parse_ignores_other_wcpayinfo_appmsg_types():
    """支付凭证等非 2000/2001 的 appmsg 不应被误判成转账/红包。"""
    receipt = (
        '<msg><appmsg><type>2002</type><title>支付凭证</title>'
        '<wcpayinfo><feedesc>¥9.90</feedesc></wcpayinfo></appmsg></msg>'
    )
    assert parse_money(receipt, 49) is None


def test_dispatch_money_events_isolated_by_account(tmp_path):
    db = Database(tmp_path / "m.db")
    db.init()
    now = datetime.now(timezone.utc)
    db.upsert_messages([
        _money_env("p_shared", "a1", "transfer", 10.0,
                   account_id="wxid_a", timestamp=now),
        _money_env("p_shared", "b1", "transfer", 20.0,
                   account_id="wxid_b", timestamp=now),
    ])
    db.upsert_rule({"account_id": "wxid_a", "peer_id": "p_shared",
                    "window_type": "daily", "limit_tokens": 100})
    cap = _Capture()
    dispatcher = AlertDispatcher({"capture": cap}, config=AlertConfig())

    sent = dispatch_money_events(dispatcher, db, "wxid_a")
    assert sent == 1
    assert len(cap.events) == 1
    # B 账号的事件不能被 A 账号的规则顺带处理掉
    assert [m["msg_id"] for m in db.pending_money_messages(account_id="wxid_b")] == ["b1"]


def test_dispatch_money_events_silences_history_before_cutoff(tmp_path):
    db = Database(tmp_path / "m.db")
    db.init()
    now = datetime.now(timezone.utc)
    db.ensure_money_alert_since("wxid_me", when=now)
    db.upsert_messages([
        _money_env("p_rule", "old", "transfer", 1.0, timestamp=now - timedelta(days=1)),
    ])
    db.upsert_rule({"account_id": "wxid_me", "peer_id": "p_rule",
                    "window_type": "daily", "limit_tokens": 100})
    cap = _Capture()
    dispatcher = AlertDispatcher({"capture": cap}, config=AlertConfig())

    assert dispatch_money_events(dispatcher, db, "wxid_me") == 0
    assert cap.events == []
    assert db.pending_money_messages() == []
