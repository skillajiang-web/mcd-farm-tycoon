#!/usr/bin/env python3
"""
省钱种田 · 命令行入口

用法示例：
    export MCD_MCP_TOKEN="你的Token"

    python cli.py status                     查看农场状态
    python cli.py crops                      查看作物与解锁条件
    python cli.py tools                      查看工具链
    python cli.py nutrition                  拉取真实餐品热量表
    python cli.py order --store 1450713 --codes 9900005466,4810
                                             用真实菜单结算一单
    python cli.py settle --original 49 --paid 36.5 --kcal 949
                                             手动结算一单
    python cli.py plant wheat                种植
    python cli.py grow                       投入全部能量推进生长
    python cli.py harvest                    收获成熟作物
    python cli.py buy tractor                购买工具
    python cli.py redeem wheat               食材兑换农场券
    python cli.py newweek                    模拟进入新的一周
    python cli.py stress                     运行经济压力测试

状态保存在同目录 farm_state.json。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from farm_engine import (  # noqa: E402
    CROPS, TOOLS, COUPON_RATE_CAP, WEEKLY_REDEEM_CAP, FarmError, FarmGame,
)

STATE_PATH = Path(__file__).parent / "farm_state.json"


def _w(s) -> int:
    """字符串的显示宽度：全角/宽字符算 2，其余算 1。

    直接用 f-string 的 <10 是按「字符个数」补空格，中文字符会少占一半宽度，
    导致含中文的表头与数据行错位。
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(s))


def pad(s, width: int, align: str = "<") -> str:
    """按显示宽度对齐填充，用于中文表格。"""
    s = str(s)
    gap = max(0, width - _w(s))
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def load() -> FarmGame:
    return FarmGame.load(STATE_PATH)


def save(game: FarmGame) -> None:
    game.save(STATE_PATH)


def guard(fn):
    """统一错误处理：规则冲突以人话输出，不抛栈。"""
    def wrapper(game, args):
        try:
            return fn(game, args)
        except FarmError as e:
            print(f"\n[规则拦截] {e}")
            return 1
        except Exception as e:  # noqa: BLE001
            print(f"\n[错误] {e}")
            return 1
    return wrapper


# ---------------------------------------------------------------- 查询类

@guard
def cmd_status(game: FarmGame, args) -> int:
    print(game.summary())
    rep = game.anti_inflation_report()
    print("\n经济健康度")
    print(f"  累计消费 / 省额 : ¥{rep['累计消费额']:.1f} / ¥{rep['累计省额']:.1f}")
    print(f"  兑换率          : {rep['兑换率']:.2%}  （上限 {COUPON_RATE_CAP:.0%}）")
    print(f"  剩余兑换额度    : ¥{game.redeem_quota():.2f}")
    print(f"  本周剩余兑换    : {WEEKLY_REDEEM_CAP - game.state.weekly_redeemed} 张")
    inv = game.state.inventory
    print(f"  食材库存        : {', '.join(f'{CROPS[k].name}×{v}' for k, v in inv.items() if v) or '空'}")
    if game.state.log:
        print("\n最近记录")
        for line in game.state.log[-5:]:
            print(f"  · {line}")
    return 0


@guard
def cmd_crops(game: FarmGame, args) -> int:
    unlocked = set(game.unlocked_crops())
    cols = [("作物", 6, "<"), ("类型", 6, "<"), ("基础能量", 11, ">"),
            ("食材价值", 10, ">"), ("解锁条件", 14, "<"), ("状态", 8, "<")]
    print("  ".join(pad(t, w, a) for t, w, a in cols).rstrip())
    print("─" * 62)
    for key, c in CROPS.items():
        kind = "种植" if c.kind == "plant" else "养殖"
        if c.unlock_from is None:
            cond = "初始可用"
        else:
            cond = f"收获{CROPS[c.unlock_from].name}×{c.unlock_count}"
        state = "已解锁" if key in unlocked else "未解锁"
        need = game.required_energy(key)
        vals = [c.name, kind, f"{c.base_energy} kcal", f"{c.food_value:.1f} 元", cond, state]
        row = "  ".join(pad(v, w, a) for v, (_, w, a) in zip(vals, cols)).rstrip()
        print(f"{row}  （当前实际需 {need} kcal）")
    return 0


@guard
def cmd_tools(game: FarmGame, args) -> int:
    owned = set(game.state.tools)
    for kind, label in (("plant", "种植工具链"), ("livestock", "养殖工具链")):
        print(f"\n{label}")
        print("  " + pad("工具", 12) + pad("价格", 8, ">") + pad("效率", 8, ">") + "  状态")
        print("  " + "─" * 36)
        for key, t in TOOLS.items():
            if t.kind != kind:
                continue
            mark = "已拥有" if key in owned else "未购买"
            print("  " + pad(t.name, 12) + pad(f"¥{t.price:.0f}", 8, ">")
                  + pad(f"-{t.efficiency:.0%}", 8, ">") + "  " + mark)
    print(f"\n种植链总效率：-{game.efficiency_for('wheat'):.0%}")
    print(f"养殖链总效率：-{game.efficiency_for('chicken'):.0%}")
    return 0


# ---------------------------------------------------------------- 资源类

@guard
def cmd_settle(game: FarmGame, args) -> int:
    r = game.settle_order(args.original, args.paid, args.kcal)
    save(game)
    print(f"已结算：省 ¥{r['saved']:.2f}，能量 +{r['energy']} kcal")
    print(game.summary())
    return 0


@guard
def cmd_order(game: FarmGame, args) -> int:
    from mcd_bridge import FarmBridge, client_from_env
    bridge = FarmBridge(client_from_env())
    bridge.load_meals(args.store)
    order = bridge.build_order([c for c in args.codes.split(",") if c.strip()])

    print("订单明细")
    for it in order["items"]:
        print("  " + pad(it["name"], 24)
              + f"¥{it['price']:>6.2f}（原价 ¥{it['original']:>6.2f}） {it['kcal']:>5} kcal")
    print("─" * 56)
    print(f"  原价合计 ¥{order['original_price']:.2f}")
    print(f"  实付合计 ¥{order['paid_price']:.2f}")
    print(f"  热量合计 {order['kcal']} kcal")

    if args.dry_run:
        print("\n（--dry-run：未写入农场状态）")
        return 0
    game.settle_order(order["original_price"], order["paid_price"], order["kcal"])
    save(game)
    print("\n已结算进农场。")
    print(game.summary())
    return 0


@guard
def cmd_nutrition(game: FarmGame, args) -> int:
    from mcd_bridge import client_from_env, parse_nutrition
    text = client_from_env().call_tool("list-nutrition-foods")
    table = parse_nutrition(text)
    print(f"共 {len(table)} 项餐品营养数据\n")
    rows = sorted(table.items(), key=lambda kv: -kv[1])
    if args.top:
        rows = rows[:args.top]
    for name, kcal in rows:
        print("  " + pad(name, 24) + f"{kcal:>5} kcal")
    return 0


# ---------------------------------------------------------------- 农场操作

@guard
def cmd_plant(game: FarmGame, args) -> int:
    game.plant(args.crop)
    save(game)
    print(f"已开始种植：{CROPS[args.crop].name}")
    print(f"当前实际需要能量：{game.required_energy(args.crop)} kcal（已扣除工具加成）")
    print(f"现有能量：{game.state.energy_pool} kcal")
    return 0


@guard
def cmd_grow(game: FarmGame, args) -> int:
    r = game.grow(args.kcal)
    save(game)
    pct = r["progress"] / r["required"] if r["required"] else 0
    bar_len = 24
    filled = int(bar_len * min(pct, 1.0))
    bar = "█" * filled + "░" * (bar_len - filled)
    print(f"[{bar}] {r['progress']}/{r['required']} kcal（{pct:.0%}）")
    print(f"本次投入 {r['invested']} kcal，能量池剩余 {r['energy_left']} kcal")
    if r["mature"]:
        print("作物已成熟，可以收获。")
    return 0


@guard
def cmd_harvest(game: FarmGame, args) -> int:
    r = game.harvest()
    save(game)
    print(f"收获 {r['crop']} ×1（该作物累计收获 {r['harvest_total']} 次）")
    print(f"库存：{r['inventory']}")
    print(f"当前已解锁：{'、'.join(r['unlocked'])}")
    return 0


@guard
def cmd_buy(game: FarmGame, args) -> int:
    r = game.buy_tool(args.tool)
    save(game)
    print(f"已购入 {r['tool']}，农资金剩余 ¥{r['fund_left']:.1f}")
    print(f"对应工具链效率现为 -{r['efficiency']:.0%}")
    return 0


@guard
def cmd_redeem(game: FarmGame, args) -> int:
    r = game.redeem(args.crop)
    save(game)
    print(f"已将 {r['crop']} 兑换为农场券")
    print(f"券池：¥{r['coupon_pool']:.1f}")
    print(f"剩余兑换额度 ¥{r['quota_left']:.2f}，本周还可兑换 {r['weekly_left']} 张")
    return 0


@guard
def cmd_newweek(game: FarmGame, args) -> int:
    game.reset_week()
    save(game)
    print(f"已进入新的一周，本周可兑换 {WEEKLY_REDEEM_CAP} 张。")
    return 0


@guard
def cmd_stress(game: FarmGame, args) -> int:
    from simulate import stress_test
    st = stress_test()
    save(game)
    return 0 if st["ok"] else 1


@guard
def cmd_reset(game: FarmGame, args) -> int:
    if not args.yes:
        print("这会清空农场存档。确认请加 --yes")
        return 1
    if STATE_PATH.exists():
        STATE_PATH.unlink()
    print("存档已清空。")
    return 0


# ---------------------------------------------------------------- 参数

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli", description="省钱种田 · 命令行")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="查看农场状态").set_defaults(func=cmd_status)
    sub.add_parser("crops", help="查看作物与解锁条件").set_defaults(func=cmd_crops)
    sub.add_parser("tools", help="查看工具链").set_defaults(func=cmd_tools)
    sub.add_parser("newweek", help="模拟进入新一周").set_defaults(func=cmd_newweek)
    sub.add_parser("stress", help="运行经济压力测试").set_defaults(func=cmd_stress)

    ps = sub.add_parser("settle", help="手动结算一单消费")
    ps.add_argument("--original", type=float, required=True, help="无券原价")
    ps.add_argument("--paid", type=float, required=True, help="券后实付")
    ps.add_argument("--kcal", type=int, required=True, help="餐品热量合计")
    ps.set_defaults(func=cmd_settle)

    po = sub.add_parser("order", help="用真实菜单结算一单")
    po.add_argument("--store", required=True, help="storeCode")
    po.add_argument("--codes", required=True, help="餐品编码，逗号分隔")
    po.add_argument("--dry-run", action="store_true", help="只计算，不写入")
    po.set_defaults(func=cmd_order)

    pn = sub.add_parser("nutrition", help="拉取餐品热量表")
    pn.add_argument("--top", type=int, default=0, help="只显示热量最高的 N 项")
    pn.set_defaults(func=cmd_nutrition)

    pp = sub.add_parser("plant", help="种植作物")
    pp.add_argument("crop", choices=list(CROPS.keys()))
    pp.set_defaults(func=cmd_plant)

    pg = sub.add_parser("grow", help="推进生长")
    pg.add_argument("kcal", type=int, nargs="?", default=None, help="投入能量，缺省为全部")
    pg.set_defaults(func=cmd_grow)

    sub.add_parser("harvest", help="收获成熟作物").set_defaults(func=cmd_harvest)

    pb = sub.add_parser("buy", help="购买工具")
    pb.add_argument("tool", choices=list(TOOLS.keys()))
    pb.set_defaults(func=cmd_buy)

    pr = sub.add_parser("redeem", help="食材兑换农场券")
    pr.add_argument("crop", choices=list(CROPS.keys()))
    pr.set_defaults(func=cmd_redeem)

    prs = sub.add_parser("reset", help="清空存档")
    prs.add_argument("--yes", action="store_true")
    prs.set_defaults(func=cmd_reset)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(load(), args)


if __name__ == "__main__":
    raise SystemExit(main())
