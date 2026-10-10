#!/usr/bin/env python3
"""
农场经济平衡模拟 —— 用真实麦当劳订单数据检验数值模型是否成立。

检验目标：
  T1. 解锁节奏合理：不出现「卡死」或「一步到位」
  T2. 农资金曲线与工具定价匹配：既能买得起，又留有追求空间
  T3. 闸门 A 全程不被击穿（这是防通胀的硬指标）
  T4. 循环可持续：连续多轮后不出现失控膨胀

基准订单取真实数据：巨无霸三件套
  原价 ¥49.0 / 现价 ¥36.5 / 热量 949 kcal（巨无霸513 + 中薯条289 + 可乐中杯147）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from farm_engine import (  # noqa: E402
    CROPS, TOOLS, COUPON_RATE_CAP, WEEKLY_REDEEM_CAP, FarmGame, FarmError,
)

# 基准订单（来自麦当劳 MCP 实测数据）
ORDER_ORIGINAL = 49.0
ORDER_PAID = 36.5
ORDER_KCAL = 949

# 现实节奏：一个普通用户每周大约消费 3 单。
# 模拟必须按此推进周次，否则会把「每周兑换上限」误判成瓶颈。
ORDERS_PER_WEEK = 3


def total_tool_cost(kind: str) -> float:
    return sum(t.price for t in TOOLS.values() if t.kind == kind)


def stress_test() -> dict:
    """压力测试：假设玩家已买齐全部工具（效率拉满），兑换率会不会击穿上限？

    这是防通胀设计的真正证明点。工具提效会压低消费额（分母），
    从而推高兑换率；若满配时仍低于上限，则任何配置下都不会击穿。
    """
    print(f"\n{'='*64}\n压力测试：满配工具下的兑换率\n{'='*64}")

    eff_plant = min(
        sum(t.efficiency for t in TOOLS.values() if t.kind == "plant"), 0.55
    )
    eff_live = min(
        sum(t.efficiency for t in TOOLS.values() if t.kind == "livestock"), 0.55
    )
    print(f"种植工具链总效率 : -{eff_plant:.0%}")
    print(f"养殖工具链总效率 : -{eff_live:.0%}")

    # 一轮完整循环（4 种作物各收获一次）所需能量与消费额
    energy = 0.0
    value = 0.0
    for key, crop in CROPS.items():
        eff = eff_plant if crop.kind == "plant" else eff_live
        energy += crop.base_energy * (1 - eff)
        value += crop.food_value

    orders = -(-energy // ORDER_KCAL)          # 向上取整
    spent = orders * ORDER_PAID
    rate = value / spent

    print(f"一轮循环所需能量 : {energy:,.0f} kcal")
    print(f"折算订单数       : {orders:.0f} 单")
    print(f"折算消费额       : ¥{spent:,.1f}")
    print(f"一轮食材总价值   : ¥{value:,.1f}")
    print(f"→ 最坏情况兑换率 : {rate:.2%}")
    print(f"→ 闸门上限       : {COUPON_RATE_CAP:.0%}")
    ok = rate < COUPON_RATE_CAP
    print(f"[{'PASS' if ok else 'FAIL'}] 满配不击穿闸门 A，余量 {COUPON_RATE_CAP - rate:.1%}")
    return {"rate": rate, "ok": ok}


def run(cycles: int = 3, verbose: bool = True) -> dict:
    game = FarmGame()
    rows = []
    gate_a_violated = False
    budget_spent = 0.0
    last_week = 0

    def order():
        nonlocal budget_spent, last_week
        game.settle_order(ORDER_ORIGINAL, ORDER_PAID, ORDER_KCAL)
        budget_spent += ORDER_PAID
        wk = game.state.order_count // ORDERS_PER_WEEK
        if wk != last_week:
            last_week = wk
            game.reset_week(log=False)

    order_no = 0
    for cycle in range(1, cycles + 1):
        if verbose:
            print(f"\n{'='*64}\n第 {cycle} 轮循环\n{'='*64}")
        # 每轮把当前解锁的作物各推进一次
        for crop_key in ["lettuce", "wheat", "chicken", "pig"]:
            if crop_key not in game.unlocked_crops():
                continue

            # 吃麦当劳直到能量够（记录单数）
            while game.state.energy_pool < game.required_energy(crop_key):
                order()
                order_no += 1

            game.plant(crop_key)
            r = game.grow()
            h = game.harvest()

            # 顺手买得起的新工具就买
            bought = []
            for tool_key, tool in TOOLS.items():
                if tool_key not in game.state.tools and game.state.farm_fund >= tool.price:
                    game.buy_tool(tool_key)
                    bought.append(tool.name)

            # 尝试兑换
            redeem_note = ""
            try:
                game.redeem(crop_key)
            except FarmError as e:
                redeem_note = "（额度不足，暂存食材）"

            rep = game.anti_inflation_report()
            if rep["闸门A是否越界"]:
                gate_a_violated = True

            rows.append({
                "轮次": cycle,
                "作物": CROPS[crop_key].name,
                "累计单数": order_no,
                "累计消费": round(game.state.total_spent, 1),
                "农资金": round(game.state.farm_fund, 1),
                "券池": round(game.state.coupon_pool, 1),
                "兑换率": f"{rep['兑换率']:.1%}",
                "本轮购入": "、".join(bought) or "—",
            })
            if verbose:
                print(
                    f"  {CROPS[crop_key].name:<4} 成熟 / 需求 {game.required_energy(crop_key):>5} kcal"
                    f" | 累计 {order_no:>2} 单 ¥{game.state.total_spent:>6.1f}"
                    f" | 农资金 ¥{game.state.farm_fund:>6.1f}"
                    f" | 券池 ¥{game.state.coupon_pool:>5.1f}"
                    f" | 兑换率 {rep['兑换率']:.1%}"
                    f" | 购入 {('、'.join(bought)) or '—'}{redeem_note}"
                )

    # 汇总校验
    rep = game.anti_inflation_report()

    print(f"\n{'='*64}\n平衡校验\n{'='*64}")
    print(f"模拟轮次        : {cycles}")
    print(f"累计订单        : {game.state.order_count} 单")
    print(f"累计消费        : ¥{game.state.total_spent:.1f}")
    print(f"累计省额        : ¥{game.state.total_saved:.1f}")
    print(f"最终农资金      : ¥{game.state.farm_fund:.1f}")
    print(f"最终券池        : ¥{game.state.coupon_pool:.1f}")
    print(f"累计兑换券值    : ¥{game.state.redeemed_value:.1f}")
    print(f"实际兑换率      : {rep['兑换率']:.2%}  （上限 {COUPON_RATE_CAP:.0%}）")
    print(f"已购工具        : {len(game.state.tools)}/{len(TOOLS)}")
    print()

    checks = [
        ("T1 解锁节奏", len(game.state.harvest_counts) == len(CROPS),
         f"已收获 {len(game.state.harvest_counts)}/{len(CROPS)} 种作物"),
        ("T2 工具可负担", len(game.state.tools) == len(TOOLS),
         f"购得 {len(game.state.tools)}/{len(TOOLS)} 件"),
        ("T3 闸门A未击穿", not gate_a_violated,
         f"实际兑换率 {rep['兑换率']:.2%} ≤ {COUPON_RATE_CAP:.0%}"),
        ("T4 券池未失控", game.state.coupon_pool < game.state.total_spent * 0.5,
         f"券池 ¥{game.state.coupon_pool:.1f} < 消费额的 50%"),
    ]
    all_ok = True
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name:<14} {detail}")
        all_ok = all_ok and ok

    print()
    print("※ T4 说明：券池只要显著低于累计消费额，即证明系统是「消费返利」")
    print("  而非「自我印钞」。若券池逼近或超过消费额，说明出现了通胀。")

    # 压力测试
    st = stress_test()
    all_ok = all_ok and st["ok"]

    return {"rows": rows, "report": rep, "all_ok": all_ok, "game": game, "stress": st}


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    result = run(cycles=n)
    print(f"\n总体结论：{'全部通过' if result['all_ok'] else '存在问题，需调参'}")
    raise SystemExit(0 if result["all_ok"] else 1)
