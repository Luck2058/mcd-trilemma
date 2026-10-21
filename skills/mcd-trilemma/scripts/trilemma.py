#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦门三路比价器 · 核价引擎。

同一份购物车，分别按「麦乐送 / 到店取餐 / 得来速」三条履约路线核价，
再用「临界时薪」把价差折算成时间成本，回答一个真实问题：

    这一趟，到底值不值得自己走？

核心不是「哪条路便宜」，而是「省下的钱 ÷ 多花的时间 = 临界时薪」。
你的时薪高于它，就别下楼；低于它，走一趟就是赚的。

三种运行模式：
    --offline   用 sample_data 的脱敏样例核价，零 Token 可跑（默认）
    --live      调用麦当劳 MCP 的真实 calculate-price 核价
    --probe     打印 tools/list 与各工具首次调用的原始返回，用于对接校验

用法::

    python trilemma.py --offline
    python trilemma.py --offline --wage 60 --walk 6
    python trilemma.py --search 麦辣 --city 成都
    MCD_MCP_TOKEN=xxx python trilemma.py --live --city 成都
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SAMPLE_DIR = HERE / "sample_data"

CHANNELS = ("delivery", "pickup", "drivethru")
CHANNEL_LABEL = {
    "delivery": "麦乐送（外送）",
    "pickup": "到店取餐",
    "drivethru": "得来速车道",
}

# 履约参数缺省值（可在 request 里覆盖）
DEFAULTS = {
    "delivery_fee": 9.0,          # 未达免配送门槛时的配送费
    "free_delivery_at": 39.0,     # 免配送门槛
    "min_order": 20.0,            # 外送起送价
    "packing_fee": 0.0,
    "delivery_eta": 32,           # 外送总时长（分钟）
    "walk_minutes": 8,            # 单程步行
    "drive_minutes": 6,           # 单程车程
    "queue_minutes": 5,           # 到店/车道排队
    "wage_per_hour": 45.0,        # 用户时薪（元/小时）
}


# --------------------------------------------------------------------- 数据模型


@dataclass
class MenuItem:
    code: str
    name: str
    price: float
    channels: tuple[str, ...] = CHANNELS


@dataclass
class Coupon:
    id: str
    title: str
    type: str                       # full_reduction | percent | free_delivery
    channels: tuple[str, ...] = CHANNELS
    threshold: float = 0.0
    amount: float = 0.0
    rate: float = 1.0
    cap: float | None = None


@dataclass
class Quote:
    channel: str
    available: bool = True
    reason: str = ""
    store: str = ""
    items_total: float = 0.0
    delivery_fee: float = 0.0
    packing_fee: float = 0.0
    coupon_id: str = ""
    coupon_title: str = ""
    coupon_discount: float = 0.0
    payable: float = 0.0
    payable_no_coupon: float = 0.0
    active_minutes: float = 0.0     # 你必须亲自花掉的时间
    total_minutes: float = 0.0      # 从下单到吃上
    detail: dict[str, Any] = field(default_factory=dict)

    def cost(self, wage_per_hour: float) -> float:
        """综合成本 = 到手价 + 主动时间的机会成本。"""
        return round(self.payable + self.active_minutes / 60.0 * wage_per_hour, 2)


# --------------------------------------------------------------------- 工具函数


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(str(value).replace("¥", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def deep_find(obj: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    """在任意嵌套结构里按候选键名找第一个非空值。

    麦当劳 MCP 各工具返回的字段命名不完全统一，用它做容错提取，
    避免把某个具体字段名写死。
    """
    lowered = {k.lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in lowered and v not in (None, "", [], {}):
                return v
        for v in obj.values():
            found = deep_find(v, keys, default=None)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = deep_find(v, keys, default=None)
            if found is not None:
                return found
    return default


def deep_find_all(obj: Any, keys: tuple[str, ...], limit: int = 500) -> list:
    lowered = {k.lower() for k in keys}
    out: list = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in lowered and isinstance(v, list) and v:
                out.extend(v)
    if out:
        return out[:limit]
    if isinstance(obj, dict):
        for v in obj.values():
            out.extend(deep_find_all(v, keys, limit))
    elif isinstance(obj, list):
        for v in obj:
            out.extend(deep_find_all(v, keys, limit))
    return out[:limit]


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------------ 离线核价器


class MockPricer:
    """基于脱敏样例数据的确定性核价器。

    履约价差来自三个真实存在的来源：
      1. 外送配送费与免配送门槛；
      2. 券池差异（外送券不能到店核销，到店券不能外送核销）；
      3. 各场景的门店覆盖不同（得来速门店最少）。
    """

    def __init__(self, menu: dict[str, MenuItem], coupons: list[Coupon], cfg: dict):
        self.menu = menu
        self.coupons = coupons
        self.cfg = cfg

    # -- 券的适用性判断 ------------------------------------------------
    @staticmethod
    def _coupon_discount(coupon: Coupon, items_total: float, delivery_fee: float) -> float:
        if items_total < coupon.threshold:
            return 0.0
        if coupon.type == "full_reduction":
            return min(coupon.amount, items_total)
        if coupon.type == "percent":
            raw = items_total * (1 - coupon.rate)
            return min(raw, coupon.cap) if coupon.cap else raw
        if coupon.type == "free_delivery":
            return delivery_fee
        return 0.0

    def _best_coupon(self, channel: str, items_total: float, delivery_fee: float):
        best = (0.0, None)
        for cp in self.coupons:
            if channel not in cp.channels:
                continue
            discount = self._coupon_discount(cp, items_total, delivery_fee)
            if discount > best[0] + 1e-9:
                best = (discount, cp)
        return best

    # -- 主流程 --------------------------------------------------------
    def quote(self, channel: str, cart: list[dict]) -> Quote:
        cfg = self.cfg
        q = Quote(channel=channel)

        # 1. 餐品可得性：外送菜单比门店菜单小
        missing = [
            c["code"]
            for c in cart
            if c["code"] not in self.menu or channel not in self.menu[c["code"]].channels
        ]
        if missing:
            q.available = False
            q.reason = "该渠道菜单不含：" + "、".join(missing)
            return q

        items_total = round(
            sum(self.menu[c["code"]].price * c["qty"] for c in cart), 2
        )
        q.items_total = items_total

        # 2. 履约费
        if channel == "delivery":
            if items_total < cfg["min_order"]:
                q.available = False
                q.reason = f"未达外送起送价 ¥{cfg['min_order']:.2f}（还差 ¥{cfg['min_order'] - items_total:.2f}）"
                return q
            delivery_fee = 0.0 if items_total >= cfg["free_delivery_at"] else cfg["delivery_fee"]
            q.delivery_fee = delivery_fee
            q.packing_fee = cfg["packing_fee"]
        else:
            q.delivery_fee = 0.0
            q.packing_fee = 0.0

        # 3. 券优化：枚举本渠道可用券，取让「到手价」最低的那张。
        #    注意免费配送券作用于配送费而非商品金额，两种抵扣不能混算。
        no_coupon_payable = round(items_total + q.delivery_fee + q.packing_fee, 2)
        best_payable, best_cp, best_saving = no_coupon_payable, None, 0.0
        for cp in self.coupons:
            if channel not in cp.channels:
                continue
            if cp.type == "free_delivery":
                if q.delivery_fee <= 0 or items_total < cp.threshold:
                    continue
                payable = round(items_total + q.packing_fee, 2)
                saving = q.delivery_fee
            else:
                deduction = self._coupon_discount(cp, items_total, q.delivery_fee)
                if deduction <= 0:
                    continue
                payable = round(items_total - deduction + q.delivery_fee + q.packing_fee, 2)
                saving = deduction
            if payable < best_payable - 1e-9:
                best_payable, best_cp, best_saving = payable, cp, saving

        q.coupon_id = best_cp.id if best_cp else ""
        q.coupon_title = best_cp.title if best_cp else ""
        q.coupon_discount = round(best_saving, 2)
        q.payable = best_payable
        q.payable_no_coupon = no_coupon_payable

        # 4. 时间账：active 只算你必须亲自花掉的时间
        if channel == "delivery":
            q.active_minutes = 0.0
            q.total_minutes = float(cfg["delivery_eta"])
        elif channel == "pickup":
            q.active_minutes = float(cfg["walk_minutes"] * 2 + cfg["queue_minutes"])
            q.total_minutes = q.active_minutes
        else:
            q.active_minutes = float(cfg["drive_minutes"] * 2 + cfg["queue_minutes"])
            q.total_minutes = q.active_minutes
        return q


# ------------------------------------------------------------------ 在线核价器


ORDER_TYPE = {"delivery": 2, "pickup": 1, "drivethru": 1}
BE_TYPE = {"delivery": 2, "pickup": 1, "drivethru": 5}
CHANNEL_CN = {"delivery": "麦乐送", "pickup": "到店自取", "drivethru": "得来速"}


def _fen(value: Any) -> float:
    """麦当劳 MCP 的金额单位是「分」，统一转成元。"""
    return round(_num(value) / 100.0, 2)


def _as_list(payload: Any) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("stores", "storeList", "list", "coupons", "couponList", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


class LivePricer:
    """调用麦当劳 MCP 真实核价。

    三条链路的参数差异（踩过才知道，这是最容易翻车的地方）：

        channel    orderType  beType  beCode
        外送       2          2       必传，来自 delivery-query-stores
        到店自取   1          1       不传，传了直接报错
        得来速     1          5       必传，来自 query-nearby-stores

    另外两个坑：
      * 金额单位是「分」不是「元」——`price: 3850` 是 ¥38.50；
      * `query-meals` 的 `data.meals` 是 `{编码: 详情}` 的字典，不是数组。
    """

    def __init__(self, client, cfg: dict, city: str = "", keyword: str = "麦当劳"):
        self.client = client
        self.cfg = cfg
        self.city = city
        self.keyword = keyword
        self._stores: dict[str, tuple[str, str]] = {}
        self._menu_cache: dict[str, dict] = {}
        self._coupon_cache: dict[str, list] = {}
        self.distance: dict[str, float] = {}
        self.trace: list[dict] = []

    # -- 底层调用 ------------------------------------------------------
    def _call(self, name: str, args: dict) -> dict:
        data = self.client.call_tool(name, args)
        self.trace.append({"tool": name, "args": args})
        return data

    @staticmethod
    def _payload(data: dict) -> Any:
        if not isinstance(data, dict):
            return data
        if data.get("success") is False:
            raise RuntimeError(f"[{data.get('code')}] {data.get('message')}")
        return data.get("data")

    # -- 门店 ---------------------------------------------------------
    def _store(self, channel: str) -> tuple[str, str]:
        if channel in self._stores:
            return self._stores[channel]

        if channel == "delivery":
            payload = self._payload(self._call("delivery-query-addresses", {}))
            addrs = (payload or {}).get("addresses") if isinstance(payload, dict) else None
            if not addrs:
                raise RuntimeError("账号下没有外送收货地址，外送链路不可用（先在外卖 App 里存一个地址）")
            address_id = deep_find(addrs[0], ("addressId", "id", "address_id"))
            payload = self._payload(
                self._call("delivery-query-stores", {"addressId": str(address_id), "beType": 2})
            )
            stores = _as_list(payload)
            if not stores:
                raise RuntimeError("该收货地址没有可配送门店")
            store = stores[0]
        else:
            data = self._call(
                "query-nearby-stores",
                {
                    "beType": BE_TYPE[channel],
                    "searchType": 2,
                    "city": self.city,
                    "keyword": self.keyword,
                },
            )
            if data.get("success") is False:
                raise RuntimeError(data.get("message") or "门店查询失败")
            stores = data.get("data") or []
            if not stores:
                label = "支持得来速车道的 " if channel == "drivethru" else ""
                raise RuntimeError(f"{self.city} 附近没有查到{label}门店")
            store = stores[0]

        code = str(deep_find(store, ("storeCode", "storeId", "code")) or "")
        be_code = str(deep_find(store, ("beCode", "businessCode")) or "")
        if channel != "pickup" and not be_code:
            raise RuntimeError(f"{CHANNEL_CN[channel]}门店未返回 beCode，无法核价")

        dist = deep_find(store, ("distance",))
        if dist is not None:
            self.distance[channel] = _num(dist)

        self._stores[channel] = (code, be_code)
        return code, be_code

    def _base_args(self, channel: str, store: str, be_code: str) -> dict:
        args: dict[str, Any] = {
            "storeCode": store,
            "orderType": ORDER_TYPE[channel],
            "beType": BE_TYPE[channel],
        }
        if channel != "pickup" and be_code:
            args["beCode"] = be_code
        return args

    # -- 菜单与券 ------------------------------------------------------
    def _menu(self, channel: str) -> dict:
        if channel in self._menu_cache:
            return self._menu_cache[channel]
        store, be_code = self._store(channel)
        payload = self._payload(self._call("query-meals", self._base_args(channel, store, be_code)))
        meals = (payload or {}).get("meals") or {}
        mapping: dict[str, dict] = {}
        for code, info in meals.items():
            if not isinstance(info, dict):
                continue
            mapping[str(code)] = {
                "name": str(info.get("name") or code),
                # 注意：query-meals 的 currentPrice 单位是「元」（字符串），
                # 与 calculate-price 的「分」不同，这里不能除 100。
                "price": _num(info.get("currentPrice")),
                "original": _num(info.get("originalPrice")),
            }
        self._menu_cache[channel] = mapping
        return mapping

    def _coupons(self, channel: str) -> list:
        if channel in self._coupon_cache:
            return self._coupon_cache[channel]
        store, be_code = self._store(channel)
        payload = self._payload(
            self._call("query-store-coupons", self._base_args(channel, store, be_code))
        )
        coupons = []
        for cp in _as_list(payload):
            cid = deep_find(cp, ("couponId", "id", "couponCode"))
            if not cid:
                continue
            coupons.append(
                {
                    "id": str(cid),
                    "title": str(deep_find(cp, ("couponName", "name", "title"), str(cid))),
                }
            )
        self._coupon_cache[channel] = coupons
        return coupons

    # -- 核价 ---------------------------------------------------------
    def _price(self, channel: str, cart: list[dict], coupon_id: str = "") -> dict:
        store, be_code = self._store(channel)
        items = [
            {"productCode": str(c["code"]), "quantity": int(c["qty"])}
            for c in cart
        ]
        if coupon_id:
            for item in items:
                item["couponId"] = coupon_id
        args = self._base_args(channel, store, be_code)
        args["items"] = items
        payload = self._payload(self._call("calculate-price", args)) or {}
        return {
            "payable": _fen(payload.get("price")),
            "items": _fen(payload.get("productPrice")),
            "discount": _fen(payload.get("discount")),
            "delivery_fee": _fen(
                deep_find(payload, ("deliveryFee", "deliveryAmount", "shippingFee"))
            ),
        }

    def _resolve(self, cart: list[dict], menu: dict) -> tuple[list[dict], list[str]]:
        """把「编码」或「名字」统一解析成菜单里真实存在的编码。"""
        resolved: list[dict] = []
        missing: list[str] = []
        for line in cart:
            key = str(line["code"])
            if key in menu:
                resolved.append({"code": key, "qty": line["qty"]})
                continue
            hits = [c for c, info in menu.items() if key in info["name"]]
            if not hits:
                missing.append(key)
                continue
            hits.sort(key=lambda c: (menu[c]["name"] != key, len(menu[c]["name"])))
            resolved.append({"code": hits[0], "qty": line["qty"]})
        return resolved, missing

    def quote(self, channel: str, cart: list[dict]) -> Quote:
        q = Quote(channel=channel)
        try:
            store, _ = self._store(channel)
            menu = self._menu(channel)
        except Exception as exc:  # noqa: BLE001 - 单条链路失败不应中断整体比价
            q.available = False
            q.reason = str(exc)
            return q

        q.store = store
        resolved, missing = self._resolve(cart, menu)
        if missing:
            q.available = False
            q.reason = "该门店菜单不含：" + "、".join(missing)
            return q

        try:
            base = self._price(channel, resolved)
        except Exception as exc:  # noqa: BLE001
            q.available = False
            q.reason = f"核价失败：{exc}"
            return q

        best = dict(base)
        best_coupon = None
        for cp in self._coupons(channel):
            try:
                priced = self._price(channel, resolved, cp["id"])
            except Exception:  # noqa: BLE001 - 单张券不可用属正常业务情况
                continue
            if priced["payable"] > 0 and priced["payable"] < best["payable"] - 0.005:
                best, best_coupon = dict(priced), cp

        q.items_total = base["items"]
        q.delivery_fee = base["delivery_fee"]
        q.payable_no_coupon = base["payable"]
        q.payable = best["payable"]
        if best_coupon:
            q.coupon_id = best_coupon["id"]
            q.coupon_title = best_coupon["title"]
            q.coupon_discount = round(base["payable"] - best["payable"], 2)
        q.detail["resolved"] = resolved

        # 时间账：优先用门店返回的真实距离推算步行时间
        walk = self.cfg["walk_minutes"]
        if self.cfg.get("auto_walk") and self.distance.get(channel):
            walk = max(1, round(self.distance[channel] / 75.0))  # 75 米/分钟 ≈ 4.5 km/h
        if channel == "delivery":
            q.active_minutes = 0.0
            q.total_minutes = float(self.cfg["delivery_eta"])
        elif channel == "pickup":
            q.active_minutes = float(walk * 2 + self.cfg["queue_minutes"])
            q.total_minutes = q.active_minutes
        else:
            q.active_minutes = float(self.cfg["drive_minutes"] * 2 + self.cfg["queue_minutes"])
            q.total_minutes = q.active_minutes
        return q


# ---------------------------------------------------------------------- 分析层


def breakeven_wage(slow: Quote, fast: Quote) -> float | None:
    """临界时薪：慢的那条路省下的钱，除以多花的时间。

    返回 None 表示两者时间相同（此时纯比价格即可）。
    """
    dt_hours = (slow.active_minutes - fast.active_minutes) / 60.0
    if abs(dt_hours) < 1e-9:
        return None
    saving = fast.payable - slow.payable
    return round(saving / dt_hours, 1)


def pareto_front(quotes: list[Quote]) -> list[str]:
    """(到手价, 主动时间) 双目标下的不可支配解集。"""
    valid = [q for q in quotes if q.available]
    front = []
    for q in valid:
        dominated = any(
            o is not q
            and o.payable <= q.payable + 1e-9
            and o.active_minutes <= q.active_minutes + 1e-9
            and (o.payable < q.payable - 1e-9 or o.active_minutes < q.active_minutes - 1e-9)
            for o in valid
        )
        if not dominated:
            front.append(q.channel)
    return front


def suggest_topup(pricer: MockPricer, cart: list[dict], channel: str = "delivery") -> dict | None:
    """外送凑单套利：花钱加购去换免配送费，到底划不划算。"""
    cfg = pricer.cfg
    base = pricer.quote(channel, cart)
    if not base.available or base.delivery_fee <= 0:
        return None
    gap = cfg["free_delivery_at"] - base.items_total
    if gap <= 0:
        return None

    cheapest = None
    in_cart = {c["code"] for c in cart}
    for code, item in pricer.menu.items():
        if channel not in item.channels or code in in_cart:
            continue
        if item.price >= gap and (cheapest is None or item.price < cheapest.price):
            cheapest = item
    if cheapest is None:
        return None

    trial = cart + [{"code": cheapest.code, "qty": 1}]
    after = pricer.quote(channel, trial)
    if not after.available:
        return None

    # 加购的边际代价 = 加购后到手价 − 加购前到手价。
    # 因为跨过门槛会免掉配送费，这个数常常远低于商品原价。
    marginal = round(after.payable - base.payable, 2)
    discounted = marginal > 0.01 and marginal < cheapest.price - 0.01
    return {
        "addCode": cheapest.code,
        "addName": cheapest.name,
        "addPrice": cheapest.price,
        "gapBefore": round(gap, 2),
        "payableBefore": base.payable,
        "payableAfter": after.payable,
        "marginalCost": marginal,
        "discounted": discounted,
        "discountPct": round(marginal / cheapest.price * 10, 1) if discounted else 10.0,
    }


# ---------------------------------------------------------------------- 渲染层


def render_markdown(result: dict) -> str:
    wage = result["wagePerHour"]
    rows = []
    quotes = {q["channel"]: q for q in result["quotes"]}
    for ch in CHANNELS:
        q = quotes[ch]
        label = CHANNEL_LABEL[ch]
        if not q["available"]:
            rows.append(f"| {label} | — | — | — | — | ❌ {q['reason']} |")
            continue
        coupon = q["couponTitle"] or "无券"
        star = " ⭐" if ch == result["recommended"] else ""
        rows.append(
            f"| {label}{star} | ¥{q['payable']:.2f} | {q['activeMinutes']:.0f} 分钟 | "
            f"¥{q['cost']:.2f} | {coupon} | ¥{q['couponDiscount']:.2f} |"
        )

    lines = [
        "### 三路到手价对比",
        "",
        f"> 假设你的时间价值 **¥{wage:.0f}/小时**，综合成本 = 到手价 + 主动时间的机会成本",
        "",
        "| 履约方式 | 到手价 | 你要花的时间 | 综合成本 | 用券 | 省下 |",
        "|---|---:|---:|---:|---|---:|",
        *rows,
        "",
    ]

    if result["breakevens"]:
        lines += ["### 临界时薪：这趟值不值得走", ""]
        for be in result["breakevens"]:
            slower = CHANNEL_LABEL[be["slower"]]
            faster = CHANNEL_LABEL[be["faster"]]
            verdict = (
                f"走 **{slower}** 划算" if wage < be["wage"] else f"别折腾，直接用 **{faster}**"
            )
            lines.append(
                f"- **{slower}** 比 {faster} 省 ¥{be['saving']:.2f}，但多花 {be['extraMinutes']:.0f} 分钟"
                f" → 临界时薪 **¥{be['wage']:.1f}/小时**。"
                f"你的时薪 ¥{wage:.0f}，结论：{verdict}。"
            )
        lines.append("")

    topup = result.get("topup")
    if topup:
        if topup["marginalCost"] <= 0.01:
            verdict = f"加购后到手价反而更低，**净省 ¥{-topup['marginalCost']:.2f}**，直接凑"
        elif topup["discounted"]:
            verdict = (
                f"实际只需要多付 **¥{topup['marginalCost']:.2f}** 就能拿到它"
                f"（相当于 **{topup['discountPct']:.1f} 折**），想加就加"
            )
        else:
            verdict = f"但要多付 ¥{topup['marginalCost']:.2f}，等于按原价买，没必要凑"
        lines += [
            "### 凑单提示",
            "",
            f"你的外送单还差 **¥{topup['gapBefore']:.2f}** 到免配送门槛（当前被收 ¥9 配送费）。"
            f"再加一份 **{topup['addName']}（原价 ¥{topup['addPrice']:.2f}）** 后，"
            f"到手价 ¥{topup['payableBefore']:.2f} → ¥{topup['payableAfter']:.2f}，{verdict}。",
            "",
        ]

    lines += [
        f"**帕累托前沿**：{', '.join(CHANNEL_LABEL[c] for c in result['pareto']) or '无'} "
        f"—— 这几条路在「更便宜」和「更省时间」之间没有对手。",
        "",
        f"*本次核价共发起 {result['apiCalls']} 次 MCP 调用 · 数据来源：{result['source']}*",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- 主流程


def build_config(request: dict) -> dict:
    cfg = dict(DEFAULTS)
    ful = request.get("fulfillment", {}) or {}
    mapping = {
        "walkMinutes": "walk_minutes",
        "driveMinutes": "drive_minutes",
        "queueMinutes": "queue_minutes",
        "deliveryEtaMinutes": "delivery_eta",
        "deliveryFee": "delivery_fee",
        "freeDeliveryAt": "free_delivery_at",
        "minOrder": "min_order",
    }
    for src, dst in mapping.items():
        if ful.get(src) is not None:
            cfg[dst] = _num(ful[src], cfg[dst])
    # 用户没显式给步行时间时，live 模式允许按门店真实距离自动推算
    cfg["auto_walk"] = ful.get("walkMinutes") is None
    if request.get("wagePerHour") is not None:
        cfg["wage_per_hour"] = _num(request["wagePerHour"], cfg["wage_per_hour"])
    return cfg


def load_sample() -> tuple[dict[str, MenuItem], list[Coupon], dict]:
    menu_raw = load_json(SAMPLE_DIR / "menu.json")
    menu = {
        item["code"]: MenuItem(
            code=item["code"],
            name=item["name"],
            price=float(item["price"]),
            channels=tuple(item.get("channels", CHANNELS)),
        )
        for item in menu_raw["items"]
    }
    coupons = [
        Coupon(
            id=c["id"],
            title=c["title"],
            type=c["type"],
            channels=tuple(c.get("channels", CHANNELS)),
            threshold=float(c.get("threshold", 0)),
            amount=float(c.get("amount", 0)),
            rate=float(c.get("rate", 1)),
            cap=c.get("cap"),
        )
        for c in menu_raw["coupons"]
    ]
    return menu, coupons, menu_raw


def run(request: dict, mode: str = "offline", city: str = "", keyword: str = "麦当劳") -> dict:
    cfg = build_config(request)
    cart = request["cart"]
    calls = 0
    trace: list[dict] = []
    distances: dict[str, float] = {}

    if mode == "offline":
        menu, coupons, _ = load_sample()
        pricer = MockPricer(menu, coupons, cfg)
        quotes = [pricer.quote(ch, cart) for ch in CHANNELS]
        topup = suggest_topup(pricer, cart)
        source = "脱敏样例数据（offline）"
    else:
        from mcp_client import McpClient  # 延迟导入：offline 模式不需要 Token

        client = McpClient()
        client.initialize()
        city = city or request.get("city", "")
        keyword = keyword or request.get("keyword", "麦当劳")
        if not city:
            raise SystemExit("live 模式需要 --city（城市名，不带“市”），例如 --city 成都")
        pricer = LivePricer(client, cfg, city=city, keyword=keyword)
        quotes = [pricer.quote(ch, cart) for ch in CHANNELS]
        topup = None
        calls = client.call_count
        trace = pricer.trace
        distances = pricer.distance
        source = f"麦当劳 MCP 实时核价（{client.url} · {city}/{keyword}）"

    valid = [q for q in quotes if q.available]
    recommended = min(valid, key=lambda q: q.cost(cfg["wage_per_hour"])).channel if valid else ""

    breakevens = []
    for slower in valid:
        for faster in valid:
            if slower.active_minutes <= faster.active_minutes + 1e-9:
                continue
            if slower.payable >= faster.payable - 1e-9:
                continue  # 又贵又慢，没有讨论价值
            w = breakeven_wage(slower, faster)
            if w is None:
                continue
            breakevens.append(
                {
                    "slower": slower.channel,
                    "faster": faster.channel,
                    "saving": round(faster.payable - slower.payable, 2),
                    "extraMinutes": round(slower.active_minutes - faster.active_minutes, 0),
                    "wage": w,
                }
            )
    breakevens.sort(key=lambda b: b["wage"])

    return {
        "wagePerHour": cfg["wage_per_hour"],
        "source": source,
        "apiCalls": calls,
        "distances": distances,
        "trace": trace,
        "recommended": recommended,
        "pareto": pareto_front(quotes),
        "breakevens": breakevens,
        "topup": topup,
        "quotes": [
            {
                "channel": q.channel,
                "label": CHANNEL_LABEL[q.channel],
                "available": q.available,
                "reason": q.reason,
                "store": q.store,
                "itemsTotal": q.items_total,
                "deliveryFee": q.delivery_fee,
                "packingFee": q.packing_fee,
                "couponId": q.coupon_id,
                "couponTitle": q.coupon_title,
                "couponDiscount": q.coupon_discount,
                "payable": q.payable,
                "payableNoCoupon": q.payable_no_coupon,
                "activeMinutes": q.active_minutes,
                "totalMinutes": q.total_minutes,
                "cost": q.cost(cfg["wage_per_hour"]),
            }
            for q in quotes
        ],
    }


def probe() -> int:
    """对接校验：打印工具清单与每个工具的原始返回，方便按真实 schema 调整字段映射。"""
    from mcp_client import McpClient

    client = McpClient()
    info = client.initialize()
    print("== serverInfo ==")
    print(json.dumps(info.get("serverInfo", {}), ensure_ascii=False, indent=2))
    print("\n== tools ==")
    tools = client.list_tools()
    for t in tools:
        print(f"- {t.get('name')}: {(t.get('description') or '')[:80]}")
    print(f"\n共 {len(tools)} 个工具\n")

    print("== 样例调用（query-meals 需先拿到 storeCode，这里仅探通路） ==")
    for name, args in [
        ("now-time-info", {}),
        ("delivery-query-addresses", {}),
        ("query-my-account", {}),
        ("available-coupons", {}),
    ]:
        try:
            print(f"\n--- {name} ---")
            print(json.dumps(client.call_tool(name, args), ensure_ascii=False)[:1200])
        except Exception as exc:  # noqa: BLE001
            print(f"失败：{exc}")
    print(f"\n实际发起 tools/call {client.call_count} 次")
    return 0


def search_menu(city: str, keyword: str, query: str, channel: str = "pickup") -> int:
    """按关键词搜门店菜单，打印真实编码与价格（写 request.json 时要用）。"""
    from mcp_client import McpClient

    client = McpClient()
    client.initialize()
    pricer = LivePricer(client, dict(DEFAULTS), city=city, keyword=keyword)
    store, be_code = pricer._store(channel)  # noqa: SLF001
    menu = pricer._menu(channel)  # noqa: SLF001

    hits = [(code, info) for code, info in menu.items() if query in info["name"]]
    print(f"门店 {store}（{city}/{keyword}）· {CHANNEL_CN[channel]} · 命中 {len(hits)} 项\n")
    for code, info in hits[:40]:
        print(f"  {code:>12}  ¥{info['price']:>6.2f}  {info['name']}")
    print(f"\n菜单共 {len(menu)} 项；本次调用 tools/call {client.call_count} 次")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="麦门三路比价器 · 外送/自取/得来速 到手价比价")
    parser.add_argument("--request", default=str(SAMPLE_DIR / "request.json"), help="请求 JSON 路径")
    parser.add_argument("--wage", type=float, default=None, help="你的时薪（元/小时）")
    parser.add_argument("--walk", type=float, default=None, help="单程步行分钟数")
    parser.add_argument("--drive", type=float, default=None, help="单程车程分钟数")
    parser.add_argument("--city", default="", help="城市名，不带“市”，如 成都（live 模式必填）")
    parser.add_argument("--keyword", default="麦当劳", help="门店搜索关键词，如 四川农业大学")
    parser.add_argument("--search", default="", help="在门店菜单里按关键词搜餐品（只打印，不核价）")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--offline", action="store_true", help="用脱敏样例核价（默认）")
    group.add_argument("--live", action="store_true", help="调用真实麦当劳 MCP 核价")
    group.add_argument("--probe", action="store_true", help="打印工具清单与原始返回")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    if args.probe:
        return probe()
    if args.search:
        if not args.city:
            raise SystemExit("--search 需要同时给 --city")
        return search_menu(args.city, args.keyword, args.search)

    request = load_json(Path(args.request))
    if args.wage is not None:
        request["wagePerHour"] = args.wage
    ful = request.setdefault("fulfillment", {})
    if args.walk is not None:
        ful["walkMinutes"] = args.walk
    if args.drive is not None:
        ful["driveMinutes"] = args.drive

    mode = "live" if args.live else "offline"
    result = run(request, mode=mode, city=args.city, keyword=args.keyword)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(result))
        print()
        print("<details><summary>原始核价数据</summary>")
        print()
        print("```json")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        print("```")
        print()
        print("</details>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
