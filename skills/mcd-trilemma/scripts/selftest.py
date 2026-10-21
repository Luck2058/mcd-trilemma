#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线自测：不需要 MCP Token，也能验证核价逻辑的正确性。

    python selftest.py

覆盖的是最容易算错的地方——优惠券的抵扣口径、渠道券池隔离、
起送/免配送门槛、时间成本折算、临界时薪与帕累托前沿。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import trilemma as T  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
    else:
        FAILED.append(f"{name}{(' — ' + detail) if detail else ''}")


def near(a: float, b: float, tol: float = 0.02) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    menu, coupons, _ = T.load_sample()
    nut = T.NutritionIndex.load_sample()
    cfg = dict(T.DEFAULTS)

    def pricer(custom: dict | None = None):
        c = dict(cfg)
        if custom:
            c.update(custom)
        return T.MockPricer(menu, coupons, c, nut)

    # ---- 1. 价格基础 -------------------------------------------------
    p = pricer()
    q = p.quote("delivery", [{"code": "BURGER_MCHICKEN", "qty": 1}, {"code": "SIDE_FRIES_M", "qty": 1}])
    check("外送餐品金额 = 25.0 + 11.5", near(q.items_total, 36.5), f"实际 {q.items_total}")
    check("未达 39 门槛收取 9 元配送费", near(q.delivery_fee, 9.0), f"实际 {q.delivery_fee}")

    # ---- 2. 免配送券口径 ---------------------------------------------
    # 样例券池里刻意不放免配送券（否则配送费永远为 0，凑单场景就消失了），
    # 这里用一张合成券单独验证口径。
    fd = T.Coupon(id="CP_TEST_FREE", title="测试免配送费", type="free_delivery", channels=("delivery",))
    free_pricer = T.MockPricer(menu, [fd], cfg)
    qf = free_pricer.quote(
        "delivery", [{"code": "BURGER_MCHICKEN", "qty": 1}, {"code": "SIDE_FRIES_M", "qty": 1}]
    )
    check(
        "免配送券只免配送费、不重复抵扣商品金额",
        near(qf.payable, 36.5) and qf.coupon_id == "CP_TEST_FREE",
        f"payable={qf.payable} coupon={qf.coupon_id}",
    )
    check("免配送券上报的节省额 = 配送费", near(qf.coupon_discount, 9.0), f"实际 {qf.coupon_discount}")

    # 主样例：满 60 免配送券不适用，退化为满 15 减 3
    check("主样例用满 15 减 3", q.coupon_id == "CP_ANY_15_3", q.coupon_id)
    check("主样例到手价 = 36.5 + 9 − 3 = 42.5", near(q.payable, 42.5), f"实际 {q.payable}")

    big = p.quote(
        "delivery",
        [{"code": "COMBO_BIGMAC", "qty": 1}, {"code": "SIDE_NUGGETS_5", "qty": 1}, {"code": "DRINK_COKE_M", "qty": 2}],
    )
    check("满 39 免配送费", near(big.delivery_fee, 0.0), f"实际 {big.delivery_fee}")
    check("满 49 减 10 优于满 15 减 3", near(big.payable, 57.5), f"实际 {big.payable}")

    # ---- 3. 起送门槛 -------------------------------------------------
    small = p.quote("delivery", [{"code": "DESSERT_PIE", "qty": 1}])
    check("未达起送价时外送标记不可用", not small.available, small.reason)

    # ---- 4. 渠道券池隔离 ---------------------------------------------
    pick = p.quote("pickup", [{"code": "BURGER_MCHICKEN", "qty": 1}, {"code": "SIDE_FRIES_M", "qty": 1}])
    dt = p.quote("drivethru", [{"code": "BURGER_MCHICKEN", "qty": 1}, {"code": "SIDE_FRIES_M", "qty": 1}])
    check("到店不可用外送券", pick.coupon_id != "CP_DLV_FREE", pick.coupon_id)
    check("到店满 30 减 8 生效", near(pick.payable, 28.5), f"实际 {pick.payable}")
    check("得来速不可用外送券", dt.coupon_id != "CP_DLV_FREE", dt.coupon_id)
    check("得来速满 39 门槛未达时退化为满 15 减 3", near(dt.payable, 33.5), f"实际 {dt.payable}")

    # ---- 5. 折扣券封顶 -----------------------------------------------
    pct = T.MockPricer(menu, [c for c in coupons if c.id == "CP_PICK_PCT85"], cfg)
    huge = pct.quote("pickup", [{"code": "COMBO_BIGMAC", "qty": 3}])
    check("8.5 折封顶 15 元", near(huge.coupon_discount, min(109.5 * 0.15, 15.0)), f"实际 {huge.coupon_discount}")

    # ---- 6. 餐品渠道可得性 -------------------------------------------
    no_bf = p.quote("delivery", [{"code": "BREAKFAST_MUFFIN", "qty": 1}])
    check("渠道菜单缺失时标记不可用", not no_bf.available, no_bf.reason)

    # ---- 7. 时间模型 -------------------------------------------------
    check("外送主动时间为 0", near(q.active_minutes, 0.0), f"实际 {q.active_minutes}")
    check("到店主动时间 = 步行往返 + 排队", near(pick.active_minutes, 8 * 2 + 5), f"实际 {pick.active_minutes}")
    check("得来速主动时间 = 车程往返 + 排队", near(dt.active_minutes, 6 * 2 + 5), f"实际 {dt.active_minutes}")

    # ---- 8. 综合成本与临界时薪 ---------------------------------------
    check("综合成本随时薪线性增长", near(q.cost(60) - q.cost(0), 0.0) and near(pick.cost(60) - pick.cost(0), 21.0), "")
    w = T.breakeven_wage(pick, q)
    check("到店 vs 外送临界时薪 = 14*60/21 = 40.0", w is not None and near(w, 14 * 60 / 21, 0.1), f"实际 {w}")
    check("同时间的两条路无临界时薪", T.breakeven_wage(q, q) is None, "")

    # ---- 9. 推荐随时薪翻转 -------------------------------------------
    req = T.load_json(T.SAMPLE_DIR / "request.json")
    low = T.run({**req, "wagePerHour": 12}, mode="offline")
    high = T.run({**req, "wagePerHour": 120}, mode="offline")
    check("时薪 12 元时推荐到店", low["recommended"] == "pickup", low["recommended"])
    check("时薪 120 元时推荐外送", high["recommended"] == "delivery", high["recommended"])

    # ---- 10. 帕累托前沿 ----------------------------------------------
    front = T.pareto_front([pick, dt, q])
    check("三条路互不支配时全部进入前沿", set(front) == {"pickup", "drivethru", "delivery"}, str(front))
    dominated = T.Quote(channel="faraway", payable=99.0, active_minutes=99.0)
    front2 = T.pareto_front([pick, dt, q, dominated])
    check("既贵又慢的方案被剔除", "faraway" not in front2, str(front2))

    # ---- 11. 凑单套利 -------------------------------------------------
    topup = T.suggest_topup(p, [{"code": "BURGER_MCHICKEN", "qty": 1}, {"code": "SIDE_FRIES_M", "qty": 1}])
    check("能识别到免配送门槛的差额", topup is not None and near(topup["gapBefore"], 2.5), str(topup))
    if topup:
        check("凑单边际成本低于商品原价", topup["marginalCost"] < topup["addPrice"], str(topup))
        check(
            "凑单边际成本 = 41.5 − 42.5 = −1.0（加购反而更便宜）",
            near(topup["marginalCost"], -1.0),
            f"实际 {topup['marginalCost']}",
        )

    # ---- 12. 无券场景 --------------------------------------------------
    bare = T.MockPricer(menu, [], cfg).quote("pickup", [{"code": "BURGER_MCHICKEN", "qty": 1}])
    check("无可用券时到手价 = 商品金额", near(bare.payable, 25.0) and near(bare.payable, bare.payable_no_coupon), "")
    check("无券时券字段为空", bare.coupon_id == "" and near(bare.coupon_discount, 0.0), "")

    # ---- 13. 营养数据匹配 ----------------------------------------------
    check("本地营养快照不少于 100 项", len(nut.rows) >= 100, str(len(nut.rows)))
    exact = nut.lookup("麦辣鸡腿汉堡")
    check("精确匹配麦辣鸡腿汉堡 485 kcal", exact["match"] == "exact" and near(exact["kcal"], 485, 1), str(exact))
    approx = nut.lookup("薯条")
    check(
        "「薯条」模糊匹配到中薯条（289 kcal）",
        approx["match"] == "approx" and approx["matched"] == "中薯条" and near(approx["kcal"], 289, 1),
        str(approx),
    )
    miss = nut.lookup("不存在的神秘汉堡")
    check("未匹配返回 none，且热量为 None 而非 0", miss["match"] == "none" and miss["kcal"] is None, str(miss))

    # ---- 14. 热量账 ----------------------------------------------------
    check("到店这单摄入 485 + 289 = 774 kcal", near(pick.kcal, 774, 2), f"实际 {pick.kcal}")
    check("到店步行往返 16 分钟消耗 56 kcal", near(pick.burned_kcal, 56, 1), f"实际 {pick.burned_kcal}")
    check("外送全程不消耗热量", near(q.burned_kcal, 0.0), f"实际 {q.burned_kcal}")
    check("得来速开车不消耗热量", near(dt.burned_kcal, 0.0), f"实际 {dt.burned_kcal}")
    check("净热量 = 摄入 − 消耗", near(pick.net_kcal, pick.kcal - pick.burned_kcal, 0.2), "")
    check("净热量不低于零", pick.net_kcal >= 0, "")

    # ---- 15. 体重影响消耗 ----------------------------------------------
    heavy = T.MockPricer(menu, coupons, {**cfg, "weight_kg": 90.0}, nut).quote(
        "pickup", [{"code": "BURGER_MCHICKEN", "qty": 1}, {"code": "SIDE_FRIES_M", "qty": 1}]
    )
    check("体重越大同样走路消耗越多", heavy.burned_kcal > pick.burned_kcal, f"{heavy.burned_kcal} vs {pick.burned_kcal}")

    # ---- 16. 凑单热量代价 ----------------------------------------------
    check("凑单提示带上加购品热量", topup is not None and topup.get("addKcal"), str(topup.get("addKcal")))

    # ---- 汇总 ---------------------------------------------------------
    total = len(PASSED) + len(FAILED)
    for line in FAILED:
        print(f"  ✗ {line}")
    print(f"\n{len(PASSED)}/{total} 通过")
    if FAILED:
        print(f"{len(FAILED)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
