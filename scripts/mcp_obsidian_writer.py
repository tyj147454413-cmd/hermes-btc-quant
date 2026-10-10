#!/usr/bin/env python3
"""
mcp_obsidian_writer.py — 极简 Obsidian 笔记「写入型」MCP 服务器（stdio JSON-RPC）

背景（2026-08-24）：项目内 mcp-obsidian 包仅只读(read_notes/search_notes)，导致
Claude Desktop 计划任务会话无法写复盘笔记（回退 data/back_review/）。本服务器
提供受控写能力：仅允许操作 vault 的 BTC_1H_Trade_Journal/*.md，文件名白名单校验，
杜绝路径穿越。由 claude_desktop_config.json 挂载（.venv/bin/python 直执行）。

工具：
  upsert_journal_note(note_name, content)  创建/覆盖笔记（返回 written 路径）
  read_journal_note(note_name)             读取笔记原文（用于写后回验）
"""
import json
import re
import sys
from pathlib import Path

VAULT_JOURNAL = Path("/Users/tangyijun/Documents/Obsidian Vault/BTC_1H_Trade_Journal")
NAME_RE = re.compile(r"^[A-Za-z0-9_\-\.]+\.md$")  # 白名单：仅库内纯文件名，禁子目录/穿越


def _safe_path(name: str) -> Path:
    if not NAME_RE.match(name or ""):
        raise ValueError(f"非法笔记名(仅允许 库内 *.md 纯文件名): {name!r}")
    return VAULT_JOURNAL / name


def upsert_journal_note(name: str, content: str) -> str:
    p = _safe_path(name)
    VAULT_JOURNAL.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(p)
    return f"written: {p} ({len(content)} chars)"


def read_journal_note(name: str) -> str:
    p = _safe_path(name)
    if not p.exists():
        raise FileNotFoundError(f"笔记不存在: {p}")
    return p.read_text(encoding="utf-8")


TOOLS = [
    {
        "name": "upsert_journal_note",
        "description": "在 Obsidian 库 BTC_1H_Trade_Journal/ 下创建或覆盖一篇笔记(整文写入)",
        "inputSchema": {
            "type": "object",
            "properties": {
                "note_name": {"type": "string", "description": "纯文件名，如 2026-08-24_0100_复盘.md"},
                "content": {"type": "string", "description": "Markdown 全文"},
            },
            "required": ["note_name", "content"],
        },
    },
    {
        "name": "read_journal_note",
        "description": "读取库内笔记全文（写后回验用）",
        "inputSchema": {
            "type": "object",
            "properties": {"note_name": {"type": "string"}},
            "required": ["note_name"],
        },
    },
]


def dispatch(name: str, args: dict) -> str:
    if name == "upsert_journal_note":
        return upsert_journal_note(args["note_name"], args["content"])
    if name == "read_journal_note":
        return read_journal_note(args["note_name"])
    raise ValueError(f"unknown tool: {name}")


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    msg["result" if error is None else "error"] = error if error is not None else result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    print("MCP Obsidian Writer Server running on stdio", flush=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method", "")
        id_ = req.get("id")
        if id_ is None:
            continue  # notification
        try:
            if method == "initialize":
                reply(id_, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "mcp-obsidian-writer", "version": "1.0.0"},
                })
            elif method == "tools/list":
                reply(id_, {"tools": TOOLS})
            elif method == "tools/call":
                params = req.get("params", {})
                try:
                    text = dispatch(params.get("name", ""), params.get("arguments", {}))
                    reply(id_, {"content": [{"type": "text", "text": text}]})
                except Exception as e:
                    reply(id_, {"content": [{"type": "text", "text": f"ERROR: {e}"}], "isError": True})
            elif method == "ping":
                reply(id_, {})
            else:
                reply(id_, error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as e:
            reply(id_, error={"code": -32603, "message": str(e)})


if __name__ == "__main__":
    main()
