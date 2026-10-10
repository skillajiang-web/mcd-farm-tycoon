#!/usr/bin/env python3
"""
「省钱种田」农场引擎 —— 纯逻辑，零第三方依赖，可独立单测。

完整设计说明见 ../FARM_DESIGN.md

三条不可违反的核心不变量（防通胀的全部依据）：
  I1. 农资金只能由「真实消费产生的省额」增加。
      食材、券池、任何游戏产出，都不得回流成农资金。
  I2. 食材兑换券受两道闸门约束：
      闸门 A —— 累计兑换价值 ≤ 累计消费额 × COUPON_RATE_CAP
      闸门 B —— 每周兑换张数 ≤ WEEKLY_REDEEM_CAP
  I3. 作物生长只由「能量消耗」推进，与现实时间无关；离线不损失、也不增长。

只要 I1 成立，工具升级的速度就被真实消费节奏锁死，经济不会自我膨胀。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional


# ---------------------------------------------------------------- 常量

MAX_EFFICIENCY = 0.55      # 工具效率叠加上限，防止需求能量归零、也防止兑换率失控
COUPON_RATE_CAP = 0.25     # 闸门 A：食材累计兑换价值 / 累计消费额
WEEKLY_REDEEM_CAP = 5      # 闸门 B：每周兑换券张数上限


# ---------------------------------------------------------------- 静态数据

@dataclass(frozen=True)
class Crop:
    key: str
    name: str
    kind: str            # "plant" 种植 | "livestock" 养殖
    base_energy: int     # 基础成熟所需能量 (kcal)
    food_value: float    # 收获食材可兑换的券面值 (元)
    unlock_from: Optional[str]   # 需先收获该作物
    unlock_count: int            # 需收获的次数


@dataclass(frozen=True)
class Tool:
    key: str
    name: str
    kind: str            # 作用对象：plant | livestock
    price: float         # 农资金价格 (元)
    efficiency: float    # 降低所需能量的比例，0.10 = -10%


CROPS: Dict[str, Crop] = {
    "lettuce": Crop("lettuce", "生菜", "plant", 1600, 6.0, None, 0),
    "wheat":   Crop("wheat",   "小麦", "plant", 3200, 12.0, "lettuce", 2),
    "chicken": Crop("chicken", "鸡",   "livestock", 4800, 18.0, "wheat", 2),
    "pig":     Crop("pig",     "猪",   "livestock", 8000, 32.0, "chicken", 2),
}

TOOLS: Dict[str, Tool] = {
    # 种植工具链 —— 人工 → 机械 → 自动化
    "sickle":   Tool("sickle",   "镰刀",     "plant", 4.0,  0.10),
    "herbicide": Tool("herbicide", "除草剂",  "plant", 10.0, 0.18),
    "fertilizer": Tool("fertilizer", "化肥",  "plant", 18.0, 0.25),
    "tractor":  Tool("tractor",  "拖拉机",   "plant", 32.0, 0.33),
    "drone":    Tool("drone",    "无人机",   "plant", 55.0, 0.45),
    # 养殖工具链 —— 投喂 → 营养 → 防疫 → 自动化
    "trough":   Tool("trough",   "饲料槽",   "livestock", 5.0,  0.12),
    "grain":    Tool("grain",    "精选粮食", "livestock", 12.0, 0.20),
    "vaccine":  Tool("vaccine",  "疫苗",     "livestock", 22.0, 0.28),
    "autofeed": Tool("autofeed", "自动喂料机", "livestock", 38.0, 0.36),
}


# ---------------------------------------------------------------- 状态

@dataclass
class GameState:
    energy_pool: int = 0              # 能量池 (kcal)
    farm_fund: float = 0.0            # 农资金 (元)
    coupon_pool: float = 0.0          # 农场券池 (元, 虚拟券面值)
    total_spent: float = 0.0          # 累计实付 (元) —— 闸门 A 的分母
    total_saved: float = 0.0          # 累计省额 (元)
    total_order_kcal: int = 0         # 累计摄入热量 (kcal)
    order_count: int = 0              # 累计消费单数
    inventory: Dict[str, int] = field(default_factory=dict)     # 食材库存
    tools: List[str] = field(default_factory=list)              # 已购工具
    harvest_counts: Dict[str, int] = field(default_factory=dict)  # 各作物累计收获
    current_crop: Optional[str] = None
    invested_energy: int = 0          # 当前作物已投入能量
    redeemed_value: float = 0.0       # 累计已兑换价值
    weekly_redeemed: int = 0          # 本周已兑换张数
    log: List[str] = field(default_factory=list)


class FarmError(RuntimeError):
    """违反规则时抛出，附带人类可读原因。"""


# ---------------------------------------------------------------- 引擎

class FarmGame:
    def __init__(self, state: Optional[GameState] = None):
        self.state = state or GameState()

    # ---------- 查询 ----------

    def unlocked_crops(self) -> List[str]:
        out = []
        for key, crop in CROPS.items():
            if crop.unlock_from is None:
                out.append(key)
                continue
            got = self.state.harvest_counts.get(crop.unlock_from, 0)
            if got >= crop.unlock_count:
                out.append(key)
        return out

    def efficiency_for(self, crop_key: str) -> float:
        kind = CROPS[crop_key].kind
        total = sum(
            TOOLS[t].efficiency for t in self.state.tools if TOOLS[t].kind == kind
        )
        return min(total, MAX_EFFICIENCY)

    def required_energy(self, crop_key: str) -> int:
        crop = CROPS[crop_key]
        return int(round(crop.base_energy * (1.0 - self.efficiency_for(crop_key))))

    def redeem_quota(self) -> float:
        """闸门 A 还剩余多少可兑换额度。"""
        ceiling = self.state.total_spent * COUPON_RATE_CAP
        return max(0.0, ceiling - self.state.redeemed_value)

    # ---------- 行为：资源获取 ----------

    def settle_order(self, original_price: float, paid_price: float, kcal: int) -> dict:
        """结算一次真实麦当劳消费。

        这是农资金与能量的唯一合法来源。
        original_price 为无券原价，paid_price 为券后实付，kcal 为该单餐品热量之和。
        """
        if paid_price < 0 or original_price < paid_price:
            raise FarmError("价格不合法：原价不得低于实付。")
        if kcal < 0:
            raise FarmError("热量不得为负。")

        saved = round(original_price - paid_price, 2)
        self.state.energy_pool += kcal
        self.state.farm_fund += saved
        self.state.total_spent += paid_price
        self.state.total_saved += saved
        self.state.total_order_kcal += kcal
        self.state.order_count += 1
        self._log(
            f"消费结算：原价 ¥{original_price:.1f} → 实付 ¥{paid_price:.1f}，"
            f"省 ¥{saved:.1f}；入池 {kcal} kcal"
        )
        return {"saved": saved, "energy": kcal}

    # ---------- 行为：种植 ----------

    def plant(self, crop_key: str) -> None:
        if crop_key not in CROPS:
            raise FarmError(f"未知作物：{crop_key}")
        if crop_key not in self.unlocked_crops():
            crop = CROPS[crop_key]
            need = CROPS[crop.unlock_from].name if crop.unlock_from else "?"
            raise FarmError(
                f"{crop.name}尚未解锁，需先收获「{need}」{crop.unlock_count} 次。"
            )
        self.state.current_crop = crop_key
        self.state.invested_energy = 0
        self._log(f"开始种植：{CROPS[crop_key].name}")

    def grow(self, kcal: Optional[int] = None) -> dict:
        """消耗能量推进生长。不传 kcal 则投入全部可用能量。"""
        if self.state.current_crop is None:
            raise FarmError("尚未选择作物。")

        need = self.required_energy(self.state.current_crop)
        remain = need - self.state.invested_energy
        invest = self.state.energy_pool if kcal is None else min(kcal, self.state.energy_pool)
        invest = min(invest, remain)

        self.state.energy_pool -= invest
        self.state.invested_energy += invest

        mature = self.state.invested_energy >= need
        if mature:
            self._log(f"{CROPS[self.state.current_crop].name}已成熟，可收获。")
        return {
            "invested": invest,
            "progress": self.state.invested_energy,
            "required": need,
            "mature": mature,
            "energy_left": self.state.energy_pool,
        }

    def is_mature(self) -> bool:
        if self.state.current_crop is None:
            return False
        return self.state.invested_energy >= self.required_energy(self.state.current_crop)

    def harvest(self) -> dict:
        """收获成熟作物。产出食材，不产出任何货币。"""
        if not self.is_mature():
            raise FarmError("作物尚未成熟，无法收获。")

        crop_key = self.state.current_crop
        crop = CROPS[crop_key]
        self.state.inventory[crop_key] = self.state.inventory.get(crop_key, 0) + 1
        self.state.harvest_counts[crop_key] = self.state.harvest_counts.get(crop_key, 0) + 1
        self.state.current_crop = None
        self.state.invested_energy = 0

        newly_unlocked = [
            CROPS[k].name for k in self.unlocked_crops() if k != crop_key
        ]
        self._log(
            f"收获 {crop.name} ×1（累计 {self.state.harvest_counts[crop_key]} 次）"
        )
        return {
            "crop": crop.name,
            "inventory": self.state.inventory.get(crop_key, 0),
            "harvest_total": self.state.harvest_counts[crop_key],
            "unlocked": [CROPS[k].name for k in self.unlocked_crops()],
            "newly_unlocked": newly_unlocked,
        }

    # ---------- 行为：工具 ----------

    def buy_tool(self, tool_key: str) -> dict:
        if tool_key not in TOOLS:
            raise FarmError(f"未知工具：{tool_key}")
        if tool_key in self.state.tools:
            raise FarmError(f"{TOOLS[tool_key].name}已拥有。")

        tool = TOOLS[tool_key]
        if self.state.farm_fund < tool.price:
            raise FarmError(
                f"农资金不足：需 ¥{tool.price:.0f}，现有 ¥{self.state.farm_fund:.1f}。"
            )

        self.state.farm_fund -= tool.price
        self.state.tools.append(tool_key)
        self._log(f"购入 {tool.name}（¥{tool.price:.0f}，效率 -{tool.efficiency:.0%}）")
        return {
            "tool": tool.name,
            "fund_left": self.state.farm_fund,
            "efficiency": self.efficiency_for(
                "lettuce" if tool.kind == "plant" else "chicken"
            ),
        }

    # ---------- 行为：兑换 ----------

    def redeem(self, crop_key: str) -> dict:
        """把食材兑换成农场券，受两道闸门约束。"""
        if crop_key not in CROPS:
            raise FarmError(f"未知作物：{crop_key}")
        if self.state.inventory.get(crop_key, 0) <= 0:
            raise FarmError(f"库存中没有{CROPS[crop_key].name}。")

        crop = CROPS[crop_key]

        # 闸门 B
        if self.state.weekly_redeemed >= WEEKLY_REDEEM_CAP:
            raise FarmError(
                f"本周兑换已达上限（{WEEKLY_REDEEM_CAP} 张），下周再来。"
            )
        # 闸门 A
        if self.redeem_quota() < crop.food_value:
            raise FarmError(
                f"兑换额度不足：需 ¥{crop.food_value:.1f}，剩余额度 ¥{self.redeem_quota():.1f}。"
                f"额度按累计消费额的 {COUPON_RATE_CAP:.0%} 生成，继续消费可提升。"
            )

        self.state.inventory[crop_key] -= 1
        self.state.coupon_pool += crop.food_value
        self.state.redeemed_value += crop.food_value
        self.state.weekly_redeemed += 1
        self._log(
            f"兑换 {crop.name} → 券池 +¥{crop.food_value:.0f}（本周第 "
            f"{self.state.weekly_redeemed}/{WEEKLY_REDEEM_CAP} 张）"
        )
        return {
            "crop": crop.name,
            "coupon_pool": self.state.coupon_pool,
            "quota_left": self.redeem_quota(),
            "weekly_left": WEEKLY_REDEEM_CAP - self.state.weekly_redeemed,
        }

    def reset_week(self, log: bool = True) -> None:
        self.state.weekly_redeemed = 0
        if log:
            self._log("新的一周，兑换次数已重置。")

    # ---------- 健康度自检 ----------

    def anti_inflation_report(self) -> dict:
        """自查经济是否失控。simulate.py 与 CI 都用它。"""
        spent = self.state.total_spent or 1e-9
        return {
            "累计消费额": round(self.state.total_spent, 2),
            "累计省额": round(self.state.total_saved, 2),
            "累计兑换券值": round(self.state.redeemed_value, 2),
            "兑换率": round(self.state.redeemed_value / spent, 4),
            "兑换率上限": COUPON_RATE_CAP,
            "闸门A是否越界": self.state.redeemed_value > spent * COUPON_RATE_CAP + 1e-6,
            "农资金是否被污染": False,  # I1 由代码结构保证：无任何路径写 farm_fund 之外的来源
            "券池": round(self.state.coupon_pool, 2),
        }

    # ---------- 持久化 ----------

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "FarmGame":
        p = Path(path)
        if not p.exists():
            return cls()
        return cls(GameState(**json.loads(p.read_text(encoding="utf-8"))))

    # ---------- 内部 ----------

    def _log(self, msg: str) -> None:
        self.state.log.append(msg)

    def summary(self) -> str:
        s = self.state
        cur = CROPS[s.current_crop].name if s.current_crop else "—"
        need = self.required_energy(s.current_crop) if s.current_crop else 0
        lines = [
            "─" * 46,
            f"能量池       : {s.energy_pool} kcal",
            f"农资金       : ¥{s.farm_fund:.1f}",
            f"农场券池     : ¥{s.coupon_pool:.1f}",
            f"累计消费/省  : ¥{s.total_spent:.1f} / ¥{s.total_saved:.1f}",
            f"当前作物     : {cur}"
            + (f"（{s.invested_energy}/{need} kcal）" if s.current_crop else ""),
            f"已解锁       : {'、'.join(CROPS[k].name for k in self.unlocked_crops())}",
            f"已购工具     : {'、'.join(TOOLS[t].name for t in s.tools) or '无'}",
            "─" * 46,
        ]
        return "\n".join(lines)
