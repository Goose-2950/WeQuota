"""转账 / 红包消息解析（M13）。

微信把转账、红包都做成 type=49 的 appmsg，金额只存在于原始 `message_content`
的 XML 里（微信 UI 不显示红包金额直到拆开）：

- 转账：`<appmsg><type>2000</type><wcpayinfo><feedesc>¥100.00</feedesc>...`
- 红包：`<appmsg><type>2001</type><wcpayinfo><sendertitle>恭喜发财</sendertitle>...`
        红包未拆前 **不含金额**，故 amount_yuan 允许为 None。

本模块只做纯解析，不依赖采集来源；直读解密副本或文件导入都复用它。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

# 与上游 huohuoer/wechat-cli 一致的 XML 安全约束，避免 XXE / 超大解析
_XML_UNSAFE_RE = re.compile(r"<!DOCTYPE|<!ENTITY", re.IGNORECASE)
_XML_PARSE_MAX_LEN = 20000
_AMOUNT_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")

TRANSFER_TYPE = "2000"
RED_PACKET_TYPE = "2001"

KIND_TRANSFER = "transfer"
KIND_RED_PACKET = "red_packet"


def _amount_yuan(text: Any) -> float | None:
    """从 `feedesc`（如 "¥100.00"）里抽出数值。"""
    if text is None:
        return None
    match = _AMOUNT_RE.search(str(text))
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _clean(text: Any) -> str | None:
    if text is None:
        return None
    value = re.sub(r"\s+", " ", str(text)).strip()
    return value or None


def _from_appmsg(appmsg: ET.Element, app_type: str, content: str) -> dict[str, Any] | None:
    # 只认微信明确的转账(2000)/红包(2001)；其它带 wcpayinfo 的 appmsg（如支付凭证）
    # 一律不算资金事件，避免误报。
    if app_type not in (TRANSFER_TYPE, RED_PACKET_TYPE):
        return None
    kind = KIND_TRANSFER if app_type == TRANSFER_TYPE else KIND_RED_PACKET
    wcpay = appmsg.find("wcpayinfo")

    amount = _amount_yuan(wcpay.findtext("feedesc")) if wcpay is not None else None
    memo = _clean(wcpay.findtext("pay_memo")) if wcpay is not None else None
    sendertitle = _clean(wcpay.findtext("sendertitle")) if wcpay is not None else None
    title = _clean(appmsg.findtext("title")) or sendertitle

    return {
        "kind": kind,
        "amount_yuan": amount,
        "memo": memo,
        "title": title,
        "raw": content,
    }


def parse_money(content: Any, base_type: int | None = None) -> dict[str, Any] | None:
    """解析一条消息的 `message_content`，命中转账/红包则返回资金事件信息。

    返回 `{"kind", "amount_yuan", "memo", "title", "raw"}`；非资金消息返回 None。
    """
    if not isinstance(content, str) or not content:
        return None
    if base_type is not None and base_type != 49:
        return None
    if "<appmsg" not in content and "<wcpayinfo" not in content:
        return None
    if len(content) > _XML_PARSE_MAX_LEN or _XML_UNSAFE_RE.search(content):
        return None
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None

    appmsg = root.find(".//appmsg")
    if appmsg is None and root.tag == "appmsg":
        appmsg = root
    if appmsg is not None:
        app_type = (appmsg.findtext("type") or "").strip()
        parsed = _from_appmsg(appmsg, app_type, content)
        if parsed is not None:
            return parsed
    return None
