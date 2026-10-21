#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""零依赖 MCP Streamable HTTP 客户端。

只依赖标准库，方便任何人 clone 下来直接跑，不需要 pip install。

用法::

    from mcp_client import McpClient
    c = McpClient()                      # 从环境变量 MCD_MCP_TOKEN 读取 Token
    c.initialize()
    print(c.list_tools())
    print(c.call_tool("query-meals", {"storeCode": "..."}))

设计要点：
1. 严格按 MCP 2025-06-18 的 Streamable HTTP 传输实现：POST + SSE 响应解析；
2. 服务端若下发 ``Mcp-Session-Id`` 响应头，后续请求自动回带；
3. ``tools/call`` 的返回既可能是 ``content[].text``（JSON 字符串），也可能是
   ``structuredContent``，这里统一成 dict，调用方不用关心差异；
4. 内置 429 限流的指数退避重试（麦当劳 MCP 限 600 次/分钟）。
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    """MCP 调用失败的统一异常。"""


class McpClient:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: int = 60):
        self.url = url or os.environ.get("MCD_MCP_URL", DEFAULT_URL)
        self.token = token or os.environ.get("MCD_MCP_TOKEN", "")
        if not self.token:
            raise McpError(
                "缺少 MCD_MCP_TOKEN。请先到 https://open.mcd.cn/mcp 申请 Token，"
                "然后 export MCD_MCP_TOKEN=xxx 再运行。"
            )
        self.timeout = timeout
        self.session_id: str | None = None
        self.server_info: dict = {}
        self._seq = 0
        self._call_count = 0

    # ------------------------------------------------------------------ 传输层

    def _next_id(self) -> int:
        self._seq += 1
        return self._seq

    def _post(self, payload: dict, retries: int = 3) -> dict | None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        last_err: Exception | None = None
        for attempt in range(retries):
            req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self.session_id = sid
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    raw = resp.read().decode("utf-8", "replace")
                if not raw.strip():
                    return None  # 通知类请求（notifications/*）返回 202 空体
                return self._parse_body(raw, ctype)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code == 429 and attempt < retries - 1:
                    wait = (2 ** attempt) + random.random()
                    time.sleep(wait)
                    last_err = McpError(f"HTTP 429 触发限流：{detail}")
                    continue
                if exc.code in (502, 503, 504) and attempt < retries - 1:
                    time.sleep(1 + attempt)
                    last_err = McpError(f"HTTP {exc.code}：{detail}")
                    continue
                raise McpError(f"HTTP {exc.code}：{detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < retries - 1:
                    time.sleep(1 + attempt)
                    last_err = McpError(f"网络错误：{exc.reason}")
                    continue
                raise McpError(f"网络错误：{exc.reason}") from exc
        raise last_err or McpError("未知错误")

    @staticmethod
    def _parse_body(raw: str, ctype: str) -> dict:
        """兼容三种返回形态：纯 JSON、SSE 流、按行分隔的 JSON。"""
        if "text/event-stream" in ctype or raw.lstrip().startswith(("event:", "data:")):
            result = None
            for block in raw.replace("\r\n", "\n").split("\n\n"):
                data_lines = [ln[5:].strip() for ln in block.split("\n") if ln.startswith("data:")]
                if not data_lines:
                    continue
                chunk = "\n".join(data_lines)
                try:
                    result = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if isinstance(result, dict) and ("result" in result or "error" in result):
                    break
            if result is None:
                raise McpError(f"无法解析 SSE 响应：{raw[:300]}")
            return result

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 兜底：按行切，取最后一个合法 JSON
            for line in reversed(raw.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        return json.loads(line)
                    except json.JSONDecodeError:
                        continue
            raise McpError(f"无法解析响应：{raw[:300]}")

    # -------------------------------------------------------------- 协议层

    def initialize(self) -> dict:
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"roots": {"listChanged": False}},
                    "clientInfo": {"name": "mcd-trilemma", "version": "1.0.0"},
                },
            }
        )
        result = (resp or {}).get("result", {})
        self.server_info = result.get("serverInfo", {})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return result

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(20):
            params = {"cursor": cursor} if cursor else {}
            resp = self._post(
                {"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list", "params": params}
            )
            result = (resp or {}).get("result", {})
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self._call_count += 1
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )
        if resp is None:
            raise McpError(f"工具 {name} 返回空响应")
        if "error" in resp:
            raise McpError(f"工具 {name} 报错：{json.dumps(resp['error'], ensure_ascii=False)}")

        result = resp.get("result", {})
        if result.get("isError"):
            raise McpError(f"工具 {name} 返回 isError=True：{json.dumps(result, ensure_ascii=False)[:400]}")

        if isinstance(result.get("structuredContent"), (dict, list)):
            return result["structuredContent"]

        texts = [
            part.get("text", "")
            for part in result.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        joined = "\n".join(t for t in texts if t.strip())
        if not joined:
            return {}
        try:
            return json.loads(joined)
        except json.JSONDecodeError:
            return {"_raw_text": joined}

    @property
    def call_count(self) -> int:
        """已发起的 tools/call 次数，用于在报告里展示核价调用成本。"""
        return self._call_count
