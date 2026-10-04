#!/usr/bin/env python3
"""patrol · 报告与告警出口。

报告：每案例一份 Markdown（候选 → 模型结论 → 完整取证记录），落 `_run/reports/`。
告警：**只有结论「可疑」才推送**（正常/无法判断只留报告——0.9 条/天的候选如果
每条都弹窗，用户会开始忽略它）。三通道：Catrace 小窗优先 → 置顶弹窗兜底 → 桌面文件留档。
只读承诺：本模块是 patrol 的写面（报告/状态/桌面告警文件），不写任何监控数据。
"""
from __future__ import annotations

import ctypes
import json
import threading
import urllib.request
from datetime import datetime
from pathlib import Path

from common import REPORTS_DIR, STATE_PATH, human, iso

CATRACE_PORTS = (23457, 23458, 23459)     # Catrace「flowwatch-alert」插件：sidecar 回环端口
CATRACE_TIMEOUT = 2.0
_POPUP_FLAGS = 0x30 | 0x1000              # MB_ICONWARNING | MB_TOPMOST
_POPUP_TIMEOUT_MS = 120_000               # 弹窗 120 秒自动关闭（用户不在场不挂住）


# ---- 报告 ----

def render_report(case: dict) -> str:
    cand = case["candidate"]
    lines = [f"# 巡检报告 · {cand['process']}",
             "",
             f"- 时间：{iso(case['ts'])}",
             f"- 窗口流量：{human(cand['total'])}（最近 60 分钟）",
             f"- 命中信号：" + "；".join(f"{s['kind']}（{s['why']}）" for s in cand["signals"]),
             f"- 取证模型：{case['provider']}",
             f"- 结论：**{case['verdict']}**（{case['elapsed']}s，{len(case['trace'])} 次工具调用）",
             "",
             "## 模型结论",
             "",
             case["answer"],
             "",
             "## 取证记录"]
    for t in case["trace"]:
        lines.append("")
        lines.append(f"### {t['name']}（{json.dumps(t['args'], ensure_ascii=False)}）")
        lines.append("```json")
        lines.append(json.dumps(t["result"], ensure_ascii=False, indent=2)[:4000])
        lines.append("```")
    return "\n".join(lines)


def save_report(case: dict) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in case["candidate"]["process"])
    path = REPORTS_DIR / f"{datetime.fromtimestamp(case['ts']):%Y%m%d-%H%M%S}_{safe}.md"
    path.write_text(render_report(case), encoding="utf-8")
    return path


# ---- 冷却状态（同进程不重复取证；也是将来「误报抑制学习」的数据源）----

def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"cases": {}}


def save_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


# ---- 告警出口（与 flowwatch 的 scripts/proxy_sentinel.py 同源；随产品自包含）----

def desktop_dir() -> Path:
    home = Path.home()
    for cand in (home / "Desktop", home / "OneDrive" / "Desktop",
                 home / "OneDrive" / "桌面", home / "桌面"):
        if cand.is_dir():
            return cand
    return home


def _catrace_notify(title: str, body: str, level: str = "warning") -> bool:
    payload = json.dumps({"title": title, "body": body, "level": level},
                         ensure_ascii=False).encode("utf-8")
    for port in CATRACE_PORTS:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/alert", data=payload,
                headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
            with urllib.request.urlopen(req, timeout=CATRACE_TIMEOUT) as resp:
                # 严格校验响应体：同端口若是别的程序（例如 Catrace 本体占着 23457），
                # 也可能回 200；只看状态码会误判成功、静默丢告警。
                if resp.status == 200:
                    try:
                        got = json.loads(resp.read(4096).decode("utf-8", "replace"))
                        if isinstance(got, dict) and got.get("ok") is True:
                            return True
                    except Exception:
                        pass
        except Exception:
            continue
    return False


def _show_popup(title: str, body: str) -> None:
    u32 = ctypes.windll.user32
    try:
        fn = getattr(u32, "MessageBoxTimeoutW", None)
        if fn is not None:
            fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                           ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
            fn.restype = ctypes.c_int
            fn(None, body, title, _POPUP_FLAGS, 0, _POPUP_TIMEOUT_MS)
            return
    except Exception:
        pass
    try:  # 老系统兜底：普通弹窗（等待被点掉）
        fn = u32.MessageBoxW
        fn.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        fn.restype = ctypes.c_int
        fn(None, body, title, _POPUP_FLAGS)
    except Exception:
        pass


def alert(title: str, body: str) -> None:
    """告警出口：桌面文件始终留档；Catrace 小窗优先，未装/未启用则置顶弹窗兜底。"""
    try:
        path = desktop_dir() / "⚠️流量巡检告警.txt"
        path.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{title}\n\n{body}\n",
                        encoding="utf-8")
    except Exception:
        pass
    if _catrace_notify(title, body):
        print("         （已推送 Catrace 小窗）")
        return
    threading.Thread(target=_show_popup, args=(title, body), name="patrol-popup").start()


def notify_case(case: dict, report_path: Path) -> bool:
    """按结论决定是否告警：**只有「可疑」弹告警**；正常 / 无法判断只留报告。"""
    if case["verdict"] != "可疑":
        return False
    cand = case["candidate"]
    sigs = "；".join(f"{s['kind']}（{s['why']}）" for s in cand["signals"])
    answer = case["answer"] or ""
    # 取结论行之后的依据段（截前 400 字），让告警里能看到"为什么可疑"
    tail = answer.split("结论", 1)[-1].strip("：: \n")[:400] if "结论" in answer else answer[:400]
    body = (f"进程：{cand['process']}\n"
            f"窗口：最近 60 分钟 {human(cand['total'])}\n"
            f"信号：{sigs}\n\n"
            f"{tail}\n\n"
            f"报告：{report_path}")
    alert(f"⚠️ 流量巡检：可疑进程 {cand['process']}", body)
    return True
