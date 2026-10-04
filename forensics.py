#!/usr/bin/env python3
"""patrol · 第二段：取证——只在候选上触发，轻量 Agent 循环产出证据链。

- 5 个只读工具（process_history / recent_events / conn_history / domain_summary /
  process_identity）；工具异常不中断回合，把错误作为观察回给模型（Pydantic AI 式）。
- 证据纪律三层：① 首轮协议层强制 tool_choice="required"；② 零工具调用提醒补查一轮；
  ③ 空结果不算证据（结论形态由系统提示词的纪律约束）。
- 模型配置复用 flowwatch 助手的 assistant_config.json（同一台机器、同一套凭据），
  可用 PATROL_PROVIDER / PATROL_BASE_URL / PATROL_API_KEY / PATROL_MODEL 覆盖；
  provider 不支持 tool_choice=required（400/404/422）时如实退回 auto，不报错。
"""
from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

from common import MAX_TURNS, config_path, connect, human, iso


# ---- provider 配置 ----

def load_provider(force_mock: bool = False, overrides: dict | None = None) -> dict:
    """读取取证用的模型配置。优先级：CLI 覆盖 > 环境变量 > assistant_config.json。"""
    overrides = overrides or {}
    if force_mock:
        return {"name": "mock", "label": "mock（不联网，仍真跑工具）",
                "base_url": "", "api_key": "", "model": "mock"}
    cfg: dict = {}
    try:
        cfg = json.loads(config_path().read_text(encoding="utf-8"))
    except Exception:
        cfg = {}
    pick = lambda key, env: (overrides.get(key) or os.environ.get(env) or cfg.get(key) or "").strip()
    name = pick("provider", "PATROL_PROVIDER").lower()
    base = pick("base_url", "PATROL_BASE_URL")
    key = pick("api_key", "PATROL_API_KEY")
    model = pick("model", "PATROL_MODEL")
    if name == "mock":
        return {"name": "mock", "label": "mock（不联网，仍真跑工具）",
                "base_url": "", "api_key": "", "model": "mock"}
    if name == "ollama":
        return {"name": "ollama", "label": f"本地 Ollama · {model or '未指定'}",
                "base_url": (base or "http://127.0.0.1:11434/v1").rstrip("/"),
                "api_key": key or "ollama", "model": model or "qwen2.5:7b"}
    if name == "openai" and base and model:
        return {"name": "openai", "label": f"远端 · {model}",
                "base_url": base.rstrip("/"), "api_key": key, "model": model}
    return {"name": "none", "label": "未配置（须先在 flowwatch 面板配模型，或用 --mock）",
            "base_url": "", "api_key": "", "model": ""}


# ---- 取证工具（5 个，全部只读）----

def tool_process_history(process: str, days: int = 7) -> dict:
    """该进程的历史流量形态：首次出现 / 近 N 天按天 / 最近 24 小时逐小时 / 峰值与中位。"""
    days = max(1, min(int(days or 7), 30))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT (bucket_ts / 3600) * 3600 AS hr, SUM(in_bytes + out_bytes)
               FROM buckets WHERE pid > 0 AND process = ? AND bucket_ts >= ?
               GROUP BY hr ORDER BY hr""",
            (process, int(time.time()) - days * 86400),
        ).fetchall()
        fs_row = conn.execute(
            "SELECT MIN(bucket_ts) FROM buckets WHERE pid > 0 AND process = ?", (process,)
        ).fetchone()
        ws_row = conn.execute(
            "SELECT MIN(bucket_ts) FROM buckets WHERE pid > 0"
        ).fetchone()
    finally:
        conn.close()
    if not rows:
        return {"process": process, "note": f"近 {days} 天没有该进程的流量记录（可能长期空闲或已卸载）"}
    history_start = iso(ws_row[0]) if ws_row and ws_row[0] else ""
    by_day: dict[str, int] = {}
    for hr, total in rows:
        day = datetime.fromtimestamp(hr).strftime("%m-%d")
        by_day[day] = by_day.get(day, 0) + total
    values = sorted(v for _h, v in rows)
    peak_hr, peak_v = max(rows, key=lambda r: r[1])
    first_seen = iso(fs_row[0]) if fs_row and fs_row[0] else ""
    note = ""
    if first_seen and first_seen <= history_start:
        note = (f"该进程首现时间早于或等于历史库记录起点（{history_start}），"
                "不能据此推断进程「新近部署」")
    return {
        "process": process,
        "first_seen_ever": first_seen,
        "history_start": history_start,
        "history_start_note": note,
        "day_totals": [{"day": d, "value": human(v)} for d, v in list(by_day.items())[-days:]],
        "recent_hours": [{"hour": datetime.fromtimestamp(hr).strftime("%m-%d %H:00"),
                          "value": human(v)} for hr, v in rows[-24:]],
        "peak_hour": {"hour": iso(peak_hr), "value": human(peak_v)},
        "median_hour": human(values[len(values) // 2]),
    }


def tool_recent_events(process: str, hours: int = 48) -> dict:
    """该进程近期的变化事件（出现 / 尖峰；vanish 对巡检无用，已滤除）。

    附 7 天 spike 频度基准：单看窗口内 15 条 spike 会显得多，但高频 spike 是很多
    常驻联网进程的日常节奏——没有基准，模型会把常态当异常（2026-10-03 实测踩过）。
    """
    hours = max(1, min(int(hours or 48), 24 * 30))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT ts, kind, detail FROM events
               WHERE process = ? AND kind IN ('appear', 'spike') AND ts >= ?
               ORDER BY ts DESC LIMIT 15""",
            (process, int(time.time()) - hours * 3600),
        ).fetchall()
        spike7 = conn.execute(
            """SELECT COUNT(*) FROM events WHERE process = ? AND kind = 'spike' AND ts >= ?""",
            (process, int(time.time()) - 7 * 86400),
        ).fetchone()[0]
    finally:
        conn.close()
    return {"process": process, "window_hours": hours,
            "note": "vanish（停止流量）与巡检判断无关，已滤除",
            "spike_count_7d": spike7,
            "spike_frequency_note": (f"该进程近 7 天共 {spike7} 次 spike（日均 {spike7 / 7:.1f} 次）。"
                                     "spike 的「倍数」以近 10 分钟为基线，分母小时倍数会虚高；"
                                     "高频 spike 常见于常驻联网进程的日常波动。"),
            "events": [{"time": iso(ts), "kind": kind, "detail": detail} for ts, kind, detail in rows]}


def tool_conn_history(process: str, hours: int = 48) -> dict:
    """该进程的连接数历史（系统连接表视角；conn_buckets 由 flowwatch 自 2026-10-04 起采集）。"""
    hours = max(1, min(int(hours or 48), 24 * 14))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT bucket_ts, conns_peak, local_conns_peak, new_per_sec_peak, peers
               FROM conn_buckets
               WHERE process = ? AND bucket_ts >= ? ORDER BY bucket_ts""",
            (process, int(time.time()) - hours * 3600),
        ).fetchall()
        earliest = conn.execute("SELECT MIN(bucket_ts) FROM conn_buckets").fetchone()
    except sqlite3.OperationalError:
        rows, earliest = [], None
    finally:
        conn.close()
    if not rows:
        return {"process": process,
                "note": "连接表里没有该进程的记录（连接快照自 2026-10-04 午间开始采集；"
                        "此前的历史不可得）"}
    peak = max(rows, key=lambda r: r[1])
    new_peak = max(rows, key=lambda r: r[3])
    try:
        peers_at_peak = json.loads(peak[4] or "{}")
    except (ValueError, TypeError):
        peers_at_peak = {}
    return {
        "process": process,
        "collection_start": iso(earliest[0]) if earliest and earliest[0] else "",
        "note": "系统连接表快照（1.5s 一次，桶内取峰值）；独立于抓包的字节视角——"
                "流量被代理/加速软件代收时，字节记在代收者头上，而本条能看出进程自己的连接形态",
        "conns_peak": {"time": iso(peak[0]), "value": peak[1]},
        "new_per_sec_peak": {"time": iso(new_peak[0]), "value": new_peak[3]},
        "peers_at_peak": peers_at_peak,   # 峰值时刻在连哪些本机端口 {端口: 连接数}
        "recent": [{"time": datetime.fromtimestamp(ts).strftime("%m-%d %H:%M"),
                    "conns": c, "local_conns": lc, "new_per_sec": n}
                   for ts, c, lc, n, _p in rows[-36:]],
    }


def tool_domain_summary(minutes: int = 60, limit: int = 10) -> dict:
    """同时段全机域名排行——只作旁证，不能声称与该进程直接关联。"""
    minutes = max(5, min(int(minutes or 60), 24 * 60))
    limit = max(1, min(int(limit or 10), 30))
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT name, kind, SUM(in_bytes + out_bytes) AS total
               FROM domains WHERE bucket_ts >= ?
               GROUP BY name, kind ORDER BY total DESC LIMIT ?""",
            (int(time.time()) - minutes * 60, limit),
        ).fetchall()
    finally:
        conn.close()
    return {"window_minutes": minutes,
            "note": "全机同时段域名排行（不与该进程直接关联，只作旁证）",
            "items": [{"name": name, "kind": kind, "value": human(total)} for name, kind, total in rows]}


def _version_strings(path: str) -> dict[str, str]:
    """读 exe 版本资源：CompanyName / ProductName / FileDescription / FileVersion。

    与 flowwatch 助手的 `_version_strings` 同源（含同一处坑：VerQueryValueW 的长度单位
    —— 二进制块按字节、字符串按字符，所以字符串一律 wstring_at 读）。patrol 自包含一份：
    它是独立产品，这段随产品走，不做跨项目私有 import。
    非 Windows 或读不到返回空字典（fail-closed）。
    """
    if sys.platform != "win32":
        return {}
    try:
        from ctypes import wintypes

        version = ctypes.WinDLL("version.dll")
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                                wintypes.DWORD, ctypes.c_void_p]
        version.GetFileVersionInfoW.restype = wintypes.BOOL
        version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                           ctypes.POINTER(ctypes.c_void_p),
                                           ctypes.POINTER(ctypes.c_uint)]
        version.VerQueryValueW.restype = wintypes.BOOL

        ignored = wintypes.DWORD()
        size = version.GetFileVersionInfoSizeW(str(path), ctypes.byref(ignored))
        if not size:
            return {}
        block = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, block):
            return {}

        def probe(sub: str) -> tuple[int, int] | None:
            pointer = ctypes.c_void_p()
            length = ctypes.c_uint()
            if not version.VerQueryValueW(block, sub, ctypes.byref(pointer), ctypes.byref(length)):
                return None
            if not pointer.value or not length.value:
                return None
            return int(pointer.value), int(length.value)

        hit = probe(r"\VarFileInfo\Translation")
        translation = ctypes.string_at(hit[0], hit[1]) if hit else b""
        pairs = [(int.from_bytes(translation[i:i + 2], "little"),
                  int.from_bytes(translation[i + 2:i + 4], "little"))
                 for i in range(0, len(translation) - 3, 4)] or [(0x0409, 0x04B0)]
        for lang, codepage in pairs:
            prefix = rf"\StringFileInfo\{lang:04x}{codepage:04x}"
            got: dict[str, str] = {}
            for field in ("CompanyName", "ProductName", "FileDescription", "FileVersion"):
                hit = probe(f"{prefix}\\{field}")
                got[field] = ctypes.wstring_at(hit[0]).strip() if hit else ""
            if any(got.values()):
                return got
        return {}
    except Exception:
        return {}


def tool_process_identity(process: str) -> dict:
    """进程身份：按名字找运行中的进程，读 exe 版本资源（厂商/产品/描述/版本）。

    注意：exe 完整路径**不进返回值**（隐私分级——聚合档不外出路径）。
    """
    try:
        import psutil
    except ImportError:
        return {"process": process, "error": "psutil 不可用，无法定位进程"}
    matches: list[dict] = []
    try:
        for p in psutil.process_iter(["pid", "name", "exe"]):
            name = p.info.get("name") or ""
            if process.lower() in name.lower():
                matches.append(p.info)
    except Exception as exc:
        return {"process": process, "error": f"枚举进程失败：{type(exc).__name__}: {exc}"}
    if not matches:
        return {"process": process, "found": False,
                "hint": "当前没有该名字的进程在运行（可能已退出）——只能按名字推断，不可靠"}
    exact = [m for m in matches if (m.get("name") or "").lower() == process.lower()]
    pick = (exact or matches)[0]
    info = _version_strings(pick.get("exe") or "") if pick.get("exe") else {}
    payload = {
        "process": process, "found": True, "matched_name": pick.get("name"),
        "pids": [m.get("pid") for m in matches][:8],
        "company": info.get("CompanyName", ""), "product": info.get("ProductName", ""),
        "description": info.get("FileDescription", ""), "version": info.get("FileVersion", ""),
    }
    if not any(payload[k] for k in ("company", "product", "description")):
        payload["note"] = "该 exe 没有版本资源（自编译 / 绿色软件常见），身份只能按名字判断"
    return payload


TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "process_history",
        "description": "查该进程的历史流量形态：全库首次出现时间、近 7 天按天合计、"
                       "最近 24 小时逐小时值、峰值小时与中位小时。判断当前流量是常态还是突破历史。",
        "parameters": {"type": "object", "properties": {
            "process": {"type": "string", "description": "进程名（与候选一致）"},
            "days": {"type": "integer", "description": "历史天数，默认 7"}},
            "required": ["process"]}}},
    {"type": "function", "function": {
        "name": "recent_events",
        "description": "查该进程近期的变化事件（出现 / 尖峰），用于判断这次流量是否伴随异常形态。",
        "parameters": {"type": "object", "properties": {
            "process": {"type": "string", "description": "进程名"},
            "hours": {"type": "integer", "description": "时间窗小时数，默认 48"}},
            "required": ["process"]}}},
    {"type": "function", "function": {
        "name": "conn_history",
        "description": "查该进程的连接数历史（系统连接表视角：活动连接数 / 指向本机连接数 / "
                       "新建速率，桶内峰值）。与流量字节相互独立——流量被代理/加速软件代收时，"
                       "字节记在代收者头上，而本条能看出进程自己的连接形态；连接数异常"
                       "（重试风暴/连接泄漏）也主要靠它。",
        "parameters": {"type": "object", "properties": {
            "process": {"type": "string", "description": "进程名"},
            "hours": {"type": "integer", "description": "时间窗小时数，默认 48"}},
            "required": ["process"]}}},
    {"type": "function", "function": {
        "name": "domain_summary",
        "description": "同时段全机域名排行（旁证：流量大致去了哪些域名）。注意这是全机数据，"
                       "不与该进程直接关联，只能作为旁证引用。",
        "parameters": {"type": "object", "properties": {
            "minutes": {"type": "integer", "description": "时间窗分钟数，默认 60"},
            "limit": {"type": "integer", "description": "返回条数，默认 10"}}}}},
    {"type": "function", "function": {
        "name": "process_identity",
        "description": "查进程身份：按名字找运行中的进程并读 exe 版本资源（厂商 / 产品 / 描述 / 版本）。"
                       "判断「这是哪个软件」必须用它，不要按文件名猜。",
        "parameters": {"type": "object", "properties": {
            "process": {"type": "string", "description": "进程名"}},
            "required": ["process"]}}},
]

TOOL_IMPLS = {
    "process_history": lambda args: tool_process_history(args.get("process", ""), args.get("days", 7)),
    "recent_events": lambda args: tool_recent_events(args.get("process", ""), args.get("hours", 48)),
    "conn_history": lambda args: tool_conn_history(args.get("process", ""), args.get("hours", 48)),
    "domain_summary": lambda args: tool_domain_summary(args.get("minutes", 60), args.get("limit", 10)),
    "process_identity": lambda args: tool_process_identity(args.get("process", "")),
}


def run_tool(name: str, args: dict) -> dict:
    """执行取证工具。参数/名称不合法都不抛异常——把错误作为观察结果回给模型。"""
    impl = TOOL_IMPLS.get(name)
    if impl is None:
        return {"error": f"未知工具：{name}"}
    try:
        return impl(args if isinstance(args, dict) else {})
    except Exception as exc:  # noqa: BLE001 - 工具失败不该打断取证
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---- LLM 调用 ----

def _post_json(url: str, payload: dict, headers: dict, timeout: float = 120.0) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def chat_completion(provider: dict, messages: list[dict], tools: list[dict] | None = None,
                    force_tool: bool = False) -> dict:
    """调一次 chat/completions，返回 assistant message（content + tool_calls）。

    force_tool=True → 协议层声明 tool_choice="required"；provider 不认（400/404/422）
    则如实退回 auto，不报错——与 flowwatch 助手的兼容策略同源。
    """
    payload: dict = {"model": provider["model"], "messages": messages, "temperature": 0.2}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "required" if force_tool else "auto"
    headers = {}
    if provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    try:
        data = _post_json(url, payload, headers)
    except urllib.error.HTTPError as exc:
        if force_tool and exc.code in (400, 404, 422):       # 这家 provider 不认 required
            payload["tool_choice"] = "auto"
            data = _post_json(url, payload, headers)
        else:
            raise
    choices = data.get("choices") or [{}]
    return choices[0].get("message") or {}


# ---- 取证循环 ----

SYSTEM_PROMPT = """你是本机流量巡检员（patrol）。上游的确定性预筛发现了一个「值得看一眼」的进程，你的任务是对它做取证并给出结论。

纪律（必须遵守）：
1. 先取证、后结论：至少调用一个工具拿到真实数据，再写结论；不许描述你没做过的动作。
2. 结论三选一：正常 / 可疑 / 无法判断。工具取不到数据时用「无法判断」，不要猜。
3. 每条依据必须引用工具返回的具体数字（例：历史峰值 809 MiB/小时、当前 4.2 倍、首现于 09-17）。
4. 判断「这是哪个软件」必须调 process_identity（读 exe 版本资源），不许按文件名猜厂商/用途。
5. domain_summary 是全机同时段数据，只能作旁证——不得声称「该进程在访问某域名」。
6. 你只做判断与建议，不做任何处置动作（不断连接、不封端口）。
7. **多数候选是正常的**——你的价值是找出少数真正可疑的，不要为了显得有用而拔高结论：
   - spike 的「倍数」不等于严重程度（基线是近 10 分钟，分母小时可虚高）；先看绝对值，
     再看该进程的 spike 频度基准（工具会给出）——符合历史节奏的波动不构成可疑。
   - 「首次出现」若早于或接近历史库记录起点，不能推断为「新近部署」。
   - 「你知道这个软件且它能解释流量」或「流量符合自身历史模式」→ 判「正常」，
     并在依据里写清是什么让它正常。真正可疑的形态是：身份不明 / 去向不明 / 无历史先例。

输出格式（严格遵守）：
结论：<正常|可疑|无法判断>
依据：
- <第一条数字证据>
- <第二条数字证据>
建议：<一句话；正常时写「无需处理」>"""


def brief(candidate: dict) -> str:
    lines = ["巡检候选（来自确定性预筛，候选 ≠ 异常）：",
             f"- 进程：{candidate['process']}",
             "- 观察窗口：最近 60 分钟",
             f"- 窗口流量：{human(candidate['total'])}",
             "- 命中信号："]
    for s in candidate["signals"]:
        lines.append(f"  - {s['kind']}：{s['why']}")
    lines.append("")
    lines.append("请按纪律取证并给出结论。")
    return "\n".join(lines)


def parse_verdict(answer: str) -> str:
    """从模型回答里提取结论标签（宽松匹配，提取不到不算数）。"""
    head = (answer or "")[:400]
    for tag in ("正常", "可疑", "无法判断"):
        if f"结论：{tag}" in head or f"结论: {tag}" in head or f"结论：**{tag}" in head:
            return tag
    return "未分类"


def run_case(candidate: dict, provider: dict, max_turns: int = MAX_TURNS) -> dict:
    """对一个候选做多步取证。最后一轮不带 tools，强制模型给结论。"""
    started = time.time()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": brief(candidate)},
    ]
    trace: list[dict] = []
    answer = ""
    nudge_left = 1                     # 首轮零工具调用时最多提醒补查 1 次（2026-10-04 实测踩过）
    for turn in range(max_turns):
        last = turn == max_turns - 1
        force = turn == 0 and not trace            # 首轮协议层强制取证（比提示词可靠）
        msg = chat_completion(provider, messages, None if last else TOOL_SCHEMAS,
                              force_tool=force)
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            if not trace and nudge_left > 0 and not last:
                # 「没查就答」是这条链路最危险的失败（flowwatch 助手的 09-20 事故同型）：
                # 补一轮明确要求先取证；空结果不算证据，但"没查"与"没查到"必须分开。
                nudge_left -= 1
                messages.append({"role": "user", "content":
                                 "你还没有调用任何工具。请先调用工具取证再下结论——"
                                 "连接形态、历史、身份、事件都可能查得到数据，"
                                 "不要因为流量为 0 就认为没有可查的证据。"})
                continue
            answer = content
            break
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            result = run_tool(name, args)
            trace.append({"name": name, "args": args, "result": result})
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(result, ensure_ascii=False)[:4000]})
    else:
        answer = "（达到取证轮数上限，未能给出完整结论——已收集的过程见取证记录）"
    grounded = any("error" not in t["result"] for t in trace) and bool(answer)
    return {"ts": int(started), "candidate": candidate, "provider": provider["label"],
            "verdict": parse_verdict(answer) if answer else "未分类",
            "answer": answer, "trace": trace, "grounded": grounded,
            "elapsed": round(time.time() - started, 1)}


def mock_case(candidate: dict) -> dict:
    """mock 取证：真跑四个工具，把事实读成一段话，明确标注 mock、不含推断。"""
    started = time.time()
    trace: list[dict] = []
    for name, args in (("process_identity", {"process": candidate["process"]}),
                       ("process_history", {"process": candidate["process"]}),
                       ("recent_events", {"process": candidate["process"]}),
                       ("conn_history", {"process": candidate["process"]})):
        result = run_tool(name, args)
        trace.append({"name": name, "args": args, "result": result})

    ident, hist, evs, conns = (t["result"] for t in trace)
    lines = ["【mock 模式】未配置真实模型：以下为工具取证结果的汇编，不含推断。",
             "", "结论：无法判断", "依据："]
    if ident.get("found"):
        who = " / ".join(x for x in (ident.get("company"), ident.get("product"),
                                     ident.get("description")) if x) or "无版本资源"
        lines.append(f"- 身份：{who}（版本 {ident.get('version') or '未知'}）")
    else:
        lines.append(f"- 身份：进程当前不在运行（{ident.get('hint', '')}）")
    if hist.get("peak_hour"):
        lines.append(f"- 历史：峰值小时 {hist['peak_hour']['value']}（{hist['peak_hour']['hour']}），"
                     f"中位小时 {hist.get('median_hour', '?')}，全库首现 {hist.get('first_seen_ever', '?')}")
    lines.append(f"- 近期事件：{len(evs.get('events', []))} 条（近 {evs.get('window_hours')} 小时）")
    if conns.get("conns_peak"):
        lines.append(f"- 连接形态：活动连接峰值 {conns['conns_peak']['value']} 条"
                     f"（{conns['conns_peak']['time']}），新建速率峰值 "
                     f"{conns['new_per_sec_peak']['value']}/秒")
    lines.append("建议：如需模型判断，请在 flowwatch 面板配置模型后重跑（或去掉 --mock）。")
    answer = "\n".join(lines)
    return {"ts": int(started), "candidate": candidate, "provider": "mock",
            "verdict": "无法判断", "answer": answer, "trace": trace, "grounded": True,
            "elapsed": round(time.time() - started, 1)}
