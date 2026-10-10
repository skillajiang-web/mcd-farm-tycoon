#!/usr/bin/env python3
"""
麦当劳 MCP 接入层 —— 把真实订单转换成农场资源。

负责：
  1. Streamable HTTP 方式连接 mcp.mcd.cn
  2. 取餐品热量（list-nutrition-foods）
  3. 取门店餐品价格（query-meals，含原价与现价）
  4. 把一单真实消费结算进农场引擎

Token 只从环境变量 MCD_MCP_TOKEN 读取，绝不落盘、绝不回显。
仅依赖 Python 标准库。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Dict, List, Optional

MCP_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    pass


# ---------------------------------------------------------------- MCP 客户端

class McdMcpClient:
    """极简 Streamable HTTP MCP 客户端。"""

    def __init__(self, token: str, url: str = MCP_URL, timeout: int = 30):
        if not token:
            raise McpError(
                "缺少 MCP Token。请先设置环境变量：\n"
                "  Windows:      set MCD_MCP_TOKEN=你的Token\n"
                '  macOS/Linux:  export MCD_MCP_TOKEN="你的Token"'
            )
        self.url = url
        self.timeout = timeout
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        self._session_id: Optional[str] = None
        self._next_id = 0
        self._initialized = False

    def _post(self, payload: dict) -> dict:
        headers = dict(self._headers)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                body = resp.read().decode("utf-8", errors="replace")
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:400]
            hint = {401: "Token 无效或已过期。", 429: "触发限流（600 次/分钟）。"}.get(e.code, "")
            raise McpError(f"HTTP {e.code} {hint}\n{detail}") from e
        except urllib.error.URLError as e:
            raise McpError(f"网络错误: {e.reason}") from e
        return self._parse_body(body, ctype)

    @staticmethod
    def _parse_body(body: str, ctype: str) -> dict:
        if "text/event-stream" in ctype:
            for line in body.splitlines():
                if line.startswith("data:"):
                    chunk = line[5:].strip()
                    if chunk:
                        try:
                            return json.loads(chunk)
                        except json.JSONDecodeError:
                            continue
            raise McpError(f"SSE 响应中没有有效 JSON：\n{body[:400]}")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise McpError(f"响应不是合法 JSON：\n{body[:400]}") from e

    def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        msg = self._post(payload)
        if "error" in msg:
            err = msg["error"]
            raise McpError(f"[{err.get('code')}] {err.get('message')}")
        return msg.get("result", {})

    def initialize(self) -> None:
        if self._initialized:
            return
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-farm-tycoon", "version": "1.0.0"},
        })
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        except McpError:
            pass
        self._initialized = True

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> str:
        self.initialize()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            raise McpError(f"工具 {name} 执行失败")
        texts = [i.get("text", "") for i in result.get("content", []) if i.get("type") == "text"]
        return "\n".join(texts) if texts else json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------- 解析

def parse_nutrition(text: str) -> Dict[str, int]:
    """解析 list-nutrition-foods 的返回文本，得到 {餐品名: 热量 kcal}。

    这个工具的返回有两层包装，是实测踩到的坑：
      外层 —— 「说明文字 + JSON」的组合文档（同 parse_meals）
      内层 —— 真正的营养表其实是 JSON 里 data 字段的一个**字符串**，
              换行以字面量 \\n 转义，直接按行切分是拿不到数据的。

    内层字符串的形态：
        [160]{productName,nutritionDescription,energyKj,energyKcal,...}:
          猪柳麦满分,null,1288,308,16,16,24,781,213
          巨无霸,null,2146,513,27,26,42,961,171
    列序：名称, 描述, 千焦, 千卡, 蛋白质, 脂肪, 碳水, 钠, 钙
    """
    raw = text.strip()

    # 第一层：剥掉说明文字，取出 JSON
    payload = raw
    idx = raw.find('{"success"')
    if idx < 0:
        idx = raw.find('{"code"')
    if idx >= 0:
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw[idx:])
            payload = (obj.get("data") or "") if isinstance(obj, dict) else raw
        except json.JSONDecodeError:
            payload = raw

    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False)

    # 第二层：逐行解析表格
    out: Dict[str, int] = {}
    for line in payload.splitlines():
        line = line.strip()
        if not line or line.startswith("["):
            continue
        parts = line.split(",")
        if len(parts) < 4:
            continue
        name = parts[0].strip()
        if not name or "*" in name or ":" in name:
            continue
        try:
            kcal = int(float(parts[3]))
        except (ValueError, IndexError):
            continue
        out[name] = kcal
    return out


def parse_meals(text: str) -> Dict[str, dict]:
    """解析 query-meals 的返回，得到 {code: {name, price, original_price}}。

    注意：MCP 工具返回的不是纯 JSON，而是「说明文字 + 原始 JSON」的组合文档，
    形如：

        # API Response Information
        Below is the response from an API call...
        ## Response Structure
        ...
        {"success":true,"code":200,...,"data":{"categories":[...],...}}

    因此必须从 `{"success"` 处开始截取，并用 raw_decode 精确截到 JSON 结束，
    不能用 find("{")（会落在说明文字里的花括号上）。
    """
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    idx = raw.find('{"success"')
    if idx < 0:
        idx = raw.find('{"code"')
    if idx < 0:
        raise McpError("query-meals 返回中找不到 JSON 主体。")

    try:
        data, _ = json.JSONDecoder().raw_decode(raw[idx:])
    except json.JSONDecodeError as e:
        raise McpError(f"query-meals 的 JSON 解析失败：{e}") from e

    meals = (data.get("data") or {}).get("meals") or {}

    out: Dict[str, dict] = {}
    for code, m in meals.items():
        try:
            price = float(m.get("currentPrice") or 0)
            original = float(m.get("originalPrice") or price)
        except (TypeError, ValueError):
            continue
        out[str(code)] = {
            "name": m.get("name", ""),
            "price": price,
            "original_price": original,
        }
    return out


# ---------------------------------------------------------------- 业务桥接

class FarmBridge:
    """把麦当劳真实订单喂给农场引擎。"""

    def __init__(self, client: McdMcpClient):
        self.c = client
        self._nutrition: Optional[Dict[str, int]] = None
        self._meals: Dict[str, dict] = {}

    def nutrition(self, refresh: bool = False) -> Dict[str, int]:
        if self._nutrition is None or refresh:
            self._nutrition = parse_nutrition(self.c.call_tool("list-nutrition-foods"))
        return self._nutrition

    def load_meals(self, store_code: str, be_type: int = 1) -> Dict[str, dict]:
        text = self.c.call_tool("query-meals", {
            "storeCode": store_code, "orderType": 1, "beType": be_type,
        })
        self._meals = parse_meals(text)
        return self._meals

    def lookup_kcal(self, meal_name: str) -> int:
        """按名称匹配热量。

        匹配优先级：完全相等 → 包含关系中最接近的一个（长度差最小）。
        营养表里有「大薯条/中薯条/小薯条」这类同名不同规格的条目，
        简单地取第一个命中会串味，必须选最接近的那个。
        """
        table = self.nutrition()
        name = (meal_name or "").strip()
        if not name:
            return 0
        if name in table:
            return table[name]

        candidates = [
            (abs(len(k) - len(name)), k, v)
            for k, v in table.items()
            if k and (k in name or name in k)
        ]
        if not candidates:
            return 0
        candidates.sort(key=lambda t: (t[0], len(t[1])))
        return candidates[0][2]

    def build_order(self, codes: List[str]) -> dict:
        """根据餐品编码组装一单：算出原价、实付、总热量。"""
        if not self._meals:
            raise McpError("尚未加载菜单，请先调用 load_meals(store_code)。")
        items, original, paid, kcal = [], 0.0, 0.0, 0
        for code in codes:
            code = code.strip()
            m = self._meals.get(code)
            if not m:
                raise McpError(f"菜单中找不到餐品编码：{code}")
            k = self.lookup_kcal(m["name"])
            items.append({"code": code, "name": m["name"], "kcal": k,
                          "price": m["price"], "original": m["original_price"]})
            original += m["original_price"]
            paid += m["price"]
            kcal += k
        return {
            "items": items,
            "original_price": round(original, 2),
            "paid_price": round(paid, 2),
            "kcal": kcal,
        }


def client_from_env() -> McdMcpClient:
    return McdMcpClient(os.environ.get("MCD_MCP_TOKEN", "").strip())
