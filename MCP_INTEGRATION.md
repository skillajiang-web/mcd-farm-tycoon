# MCP_INTEGRATION.md

本文件说明「省钱种田」实际调用的麦当劳 MCP Server、Tool、调用流程与业务价值。

## 1. MCP Server 信息

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp`（麦当劳中国 MCP Server） |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 协议版本 | `2025-06-18` |
| 限流 | 每 Token 每分钟 600 次请求，超限返回 `429` |

标准配置（不含真实 Token）：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": {
        "Authorization": "Bearer YOUR_MCP_TOKEN"
      }
    }
  }
}
```

> 本项目代码通过环境变量 `MCD_MCP_TOKEN` 读取 Token，**不在任何文件中硬编码凭证**。

## 2. 实际使用的 Tool 清单

| # | Tool Name | 在本项目中的作用 | 关键入参 |
|---|---|---|---|
| 1 | `query-nearby-stores` | 定位到店自取门店，取得 `storeCode` | `beType=1`, `searchType=2`, `city`, `keyword` |
| 2 | `auto-bind-coupons` | **先领券**，否则省额被低估 | 无 |
| 3 | `available-coupons` | 播报当前可领券，与农场券池联动提示 | 无 |
| 4 | `query-meals` | 取门店餐品的**原价与现价**，用于计算省额 | `storeCode`, `orderType`, `beType` |
| 5 | `list-nutrition-foods` | 取餐品 `energyKcal`，这是作物生长的唯一燃料 | 无 |
| 6 | `calculate-price` | 校验券后实付金额，确保省额口径准确 | `storeCode`, `beType`, `orderType`, `products[]` |
| 7 | `query-meal-detail` | （推荐增强）拆解套餐组成以精确计算热量 | `storeCode`, `orderType`, `beType`, `code` |
| 8 | `query-order` | 校验订单真实完成，防止伪造消费刷能量 | `orderId` |

### 工具选择理由

- **为什么用 `list-nutrition-foods` 而不是估算热量？**
  热量在本项目里是「货币」——它直接决定作物多久成熟。任何估算都会失真。
  实测该工具返回 158 项餐品的精确 `energyKcal`（如巨无霸 513、中薯条 289）。

- **为什么 `auto-bind-coupons` 必须先执行？**
  省额 = 原价 − 实付。若不先领券，实付偏高、省额偏低，玩家辛苦消费却拿不到应有的农资金。
  实测：不领券时 `query-my-coupons` 为空，领券后成功获得 6 张券。

- **为什么需要 `query-order`？**
  能量与农资金只应由**真实发生的消费**产生。若仅凭用户口述就发放资源，
  整个经济系统可被伪造。`query-order` 提供订单真实性校验。

- **`query-meal-detail` 是推荐的精度增强**：
  当前版本用名称模糊匹配热量，「巨无霸三件套」会被匹配到「巨无霸」，
  只计主食热量。用 `query-meal-detail` 拆解套餐组成后累加，可得到精确值。
  这是已知限制，已在 `FARM_DESIGN.md` 第 8 节记录。

## 3. 调用流程

### 流程 A：订单结算（资源入口）

```
query-nearby-stores(beType=1)    取得 storeCode
        ↓
auto-bind-coupons                先领券，保证省额真实
        ↓
query-meals(storeCode)           取餐品 originalPrice / currentPrice
        ↓
list-nutrition-foods             取餐品 energyKcal
        ↓
calculate-price                  校验券后实付
        ↓
结算：能量 += Σkcal
      农资金 += Σ(原价 − 实付)
```

**这一条是全部资源的唯一入口。** 农场内没有任何其他途径可以增加农资金。

### 流程 B：种植 → 成熟 → 收获

```
检查解锁条件（收获上一级作物 ×2）
        ↓
实际所需能量 = 基础能量 × (1 − 工具链效率)
        ↓
plant → grow（消耗能量池）
        ↓
能量达标 → mature → harvest → 食材入仓
```

### 流程 C：兑换（受两道闸门约束）

```
检查闸门 B：本周兑换 < 5 张？
        ↓
检查闸门 A：累计兑换 + 本次价值 ≤ 累计消费额 × 25%？
        ↓
通过 → 食材 −1，券池 += 食材价值
        ↓
券池达标 → 提示用户调 auto-bind-coupons 领取真实麦当劳券
```

## 4. 业务价值

### 4.1 与常规优惠工具的区别

| 维度 | 常规领券工具 | 省钱种田 |
|---|---|---|
| 用户动机 | 被动（想起来才用） | 主动（作物快熟了，想吃） |
| 反馈周期 | 即时但无积累 | 有进度条、有里程碑 |
| 长期留存 | 弱 | 强（工具体系 + 关卡解锁） |
| 省钱感知 | 「省了 12 块」 | 「我的小麦熟了，还攒了 ¥6 券」 |

### 4.2 量化价值

以实测数据估算（2026-10-10，门店 1450713）：

- 单次典型消费：巨无霸三件套 + 薯条 = 原价 ¥62.5 → 实付 ¥50.0，**省 ¥12.5**
- 一轮完整通关（4 种作物）约需 18 单 → 累计消费约 ¥900，累计省约 ¥225
- 期间可获得全部 9 件工具，并积累农场券池

**关键价值不是「省了几块钱」，而是「把省钱变成了用户愿意持续回来的理由」。**
常规优惠工具的流失点是「用完就走」，本项目通过农场的进度条与解锁链，
把一次性优惠转化成长期习惯。

### 4.3 防通胀设计的价值

用户需求是「收获可兑换券、再用于消费」，这构成闭环，天然有通胀风险。
本项目用两道闸门锁定：

- 额度闸门：累计兑换 ≤ 消费额 × 25%
- 节奏闸门：每周 ≤ 5 张

压力测试证明：即使玩家买齐全部工具（效率拉满 55%），兑换率也只有 20.70%，
低于 25% 上限，余量 4.3%。**这是本项目在机制设计上最核心的贡献。**

## 5. 技术实现说明

主体代码 `scripts/`，**仅依赖 Python 标准库**，无需第三方包。

实现的 MCP 协议要点：

1. `initialize` 握手 + `notifications/initialized` 通知
2. `tools/call` 调用，解析 `content[].text`
3. 会话管理：从响应头 `Mcp-Session-Id` 读取并透传
4. 兼容 `application/json` 与 `text/event-stream`（SSE `data:` 行）两种响应体
5. 错误分类：`401`（Token 失效）、`429`（限流）

### 开发中踩到的两个真实坑（值得记录）

**坑 1：MCP 工具返回的是「说明文档 + JSON」，不是纯 JSON。**

`query-meals` 的返回形如：

```
# API Response Information

Below is the response from an API call...

## Response Structure

> Content-Type: application/json
- **data**: 餐品列表...

## Original Response

{"success":true,"code":200,...,"data":{"categories":[...]}}
```

若用 `find("{")` 定位 JSON，会命中说明文字里的花括号导致解析失败。
正确做法：定位 `{"success"` 后用 `JSONDecoder().raw_decode()` 精确截取。

**坑 2：营养表被「双重包装」——真正的数据是 JSON 里一个转义过的字符串。**

`list-nutrition-foods` 的 `data` 字段是一个**字符串**，内容为 CSV 风格的表格，
换行以字面量 `\n` 转义。直接对返回值按行切分，一行都取不到（实测得到 0 条）。
必须先用 `json.loads` 取出 `data`，再对它 `splitlines()` 逐行解析。
修复后成功解析 158 项（巨无霸 513 kcal，与官方一致）。

### 运行验证（真实调用，全部通过）

```bash
export MCD_MCP_TOKEN="你的Token"

python scripts/cli.py nutrition                 # 成功解析 158 项营养数据
python scripts/cli.py order --store 1450713 --codes 9900005466,4810
                                                # 成功结算 802 kcal + ¥12.5
python scripts/simulate.py 6                    # 5 项校验全部 PASS
```

## 6. 合规与安全

- Token 仅从环境变量读取，代码与文档中不含任何真实凭证。
- 本项目**不自动下单**。`create-order` 不在自动调用链内，所有支付均由用户确认。
- 能量与农资金需绑定真实订单，不作本地凭空发放。
- 农场券为游戏内虚拟券，已在 README 与 SKILL 中明示，不宣称可替代官方优惠凭证。
