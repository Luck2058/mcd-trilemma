#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""营养数据索引：把 MCP 返回的营养表匹配到菜单餐品。

为什么需要单独一层：
`list-nutrition-foods` 返回的是一张「文本表格」（首行是表头，之后是逗号分隔的行），
而菜单里的餐品名与营养库的产品名**并不完全一致**——菜单叫「薯条」，营养库有
「中薯条 / 大薯条 / 小薯条」；菜单叫「可乐」，营养库是「可乐中杯 / 可乐大杯 / 可乐小杯」。

所以这里做三档匹配，并把匹配质量如实标出来（exact / approx / none），
绝不假装精确——热量数字如果来源不清，用户就会拿它做错误决策。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SAMPLE_DIR = Path(__file__).resolve().parent / "sample_data"

# 模糊匹配时，尺寸词的偏好顺序（中杯最常见，作为默认）
_SIZE_PREF = ("中杯", "中", "大杯", "小杯", "小", "大")


def normalize(name: str) -> str:
    text = str(name or "").strip().lower()
    text = re.sub(r"[\s（）()【】\[\]·、,，]+", "", text)
    return text


def _char_overlap(a: str, b: str) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


class NutritionIndex:
    """餐品名 → 营养数据 的匹配索引。"""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self._by_name: dict[str, dict] = {}
        for row in rows:
            key = normalize(row.get("productName", ""))
            if key:
                self._by_name.setdefault(key, row)

    # -- 构建 ---------------------------------------------------------
    @classmethod
    def load_sample(cls) -> "NutritionIndex":
        path = SAMPLE_DIR / "nutrition.json"
        if not path.exists():
            return cls([])
        with path.open("r", encoding="utf-8") as fh:
            return cls(json.load(fh).get("items", []))

    @classmethod
    def from_mcp(cls, client) -> "NutritionIndex":
        """解析 `list-nutrition-foods` 返回的文本表格。"""
        payload = client.call_tool("list-nutrition-foods", {})
        raw = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(raw, str):
            raw = json.dumps(raw, ensure_ascii=False)

        match = re.search(r"\{([^}]*)\}", raw)
        if not match:
            return cls([])
        columns = [c.strip() for c in match.group(1).split(",")]

        rows: list[dict] = []
        for line in raw[match.end():].splitlines():
            cells = [c.strip() for c in line.strip().split(",")]
            if len(cells) != len(columns):
                continue
            rows.append(dict(zip(columns, cells)))
        return cls(rows)

    # -- 查询 ---------------------------------------------------------
    def lookup(self, name: str) -> dict:
        """返回 {kcal, protein, fat, carb, sodium, match, matched}。

        match ∈ {exact, approx, none}；none 时 kcal 为 None，
        调用方必须如实显示「未匹配」，不能当 0 处理。
        """
        key = normalize(name)
        if not key or not self._by_name:
            return self._miss()

        row = self._by_name.get(key)
        if row:
            return self._hit(row, "exact")

        # 子串双向匹配 + 字符重叠度打分
        best: tuple[float, dict] | None = None
        for cand_key, cand in self._by_name.items():
            if key in cand_key or cand_key in key:
                score = 0.6 + min(len(key), len(cand_key)) / max(len(key), len(cand_key)) * 0.3
            else:
                score = _char_overlap(key, cand_key)
            if score < 0.6:
                continue
            # 同分时按尺寸词偏好排序，避免「可乐」匹配到「可乐大杯」
            penalty = 0.0
            for i, token in enumerate(_SIZE_PREF):
                if token in cand_key and token not in key:
                    penalty = i * 0.01
                    break
            score -= penalty
            if best is None or score > best[0]:
                best = (score, cand)

        if best is None:
            return self._miss()
        return self._hit(best[1], "approx")

    def _hit(self, row: dict, kind: str) -> dict:
        def num(key: str) -> float | None:
            value = row.get(key)
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        return {
            "kcal": num("energyKcal"),
            "protein": num("protein"),
            "fat": num("fat"),
            "carb": num("carbohydrate"),
            "sodium": num("sodium"),
            "match": kind,
            "matched": row.get("productName", ""),
        }

    @staticmethod
    def _miss() -> dict:
        return {
            "kcal": None,
            "protein": None,
            "fat": None,
            "carb": None,
            "sodium": None,
            "match": "none",
            "matched": "",
        }
