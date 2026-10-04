#!/usr/bin/env python3
"""patrol —— 本机流量巡检：确定性预筛 + Agent 取证。

完整的产品说明与实测记录见 README.md。两段式设计：
  · screen.py    预筛：字节信号（量级突破/新面孔/绝对巨量）+ 连接信号（连接数/新建速率），
                 确定性、只读 flowwatch 历史库、零模型成本——产出「值得看一眼」的候选；
  · forensics.py 取证：只在候选上触发，轻量 Agent 循环（5 个只读工具）→ 结论 + 证据链，
                 结论只三选一：正常 / 可疑 / 无法判断。

用法:
    python patrol.py                          # 只做预筛（dry-run，零模型成本）
    python patrol.py --check                  # 完整巡逻：预筛 → 取证 → 报告 → 按结论告警
    python patrol.py --check --mock           # 同上，mock 取证（不联网，验证链路）
    python patrol.py --check --case xxx.exe   # 手动指定候选（测试取证链路）
    python patrol.py --replay 168             # 回测最近 168 小时（只做预筛）
    python patrol.py --test-alert             # 弹一次测试告警（验证通道）
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from common import (
    ABS_BIG, CONN_DAYS, CONN_MIN, COOLDOWN_HOURS, CRON_LOG, FACTOR, HIST_DAYS,
    MAX_TURNS, MIN_CUR, NEW_MIN, NEW_MIN_RATE, RUN_DIR, WINDOW,
    __version__, connect, human, iso,
)
from forensics import load_provider, mock_case, run_case
from report import alert, load_state, notify_case, save_report, save_state
from screen import (
    conn_peaks_by_process, conn_series_by_process, first_seen_by_process,
    hourly_by_process, judge, judge_conns, replay, screen, window_by_process,
)


def do_check(args: argparse.Namespace) -> int:
    """完整巡逻：预筛（或手动候选）→ 冷却过滤 → 取证 → 报告 → 按结论告警。"""
    provider = load_provider(force_mock=args.mock,
                             overrides={"provider": args.provider, "base_url": args.base_url,
                                        "api_key": args.api_key, "model": args.model})
    if provider["name"] == "none":
        print(f"（{provider['label']}）")
        return 2

    now = int(time.time())
    if args.case:
        # 手动候选也跑真实的信号判定（字节 + 连接）——brief 贫瘠会让模型误判"没数据可查"
        conn = connect()
        try:
            w0 = now - WINDOW
            total = window_by_process(conn, w0, now).get(args.case, 0)
            cur_conn = conn_peaks_by_process(conn, w0, now).get(args.case)
            hist = hourly_by_process(conn, now - HIST_DAYS * 86400 - WINDOW, w0).get(args.case) or {}
            first_seen = first_seen_by_process(conn).get(args.case)
            try:
                chist = conn_series_by_process(conn, now - CONN_DAYS * 86400 - WINDOW, w0).get(args.case)
            except sqlite3.OperationalError:
                chist = None
        finally:
            conn.close()
        sigs = judge(args.case, total, list(hist.values()), first_seen, w0,
                     args.factor, args.min_cur, args.new_min, args.abs_big)
        if cur_conn:
            sigs += judge_conns(args.case, cur_conn[0], cur_conn[1], chist,
                                conn_min=args.conn_min, new_rate_min=args.new_rate_min)
        signals = [{"kind": k, "why": w} for k, w in sigs]
        if not signals:
            signals = [{"kind": "手动指定",
                        "why": "--case 指定；当前窗口未命中任何阈值（仍按要求取证）"}]
        cands = [{"process": args.case, "total": total, "signals": signals}]
    else:
        cands = screen(now, factor=args.factor, min_cur=args.min_cur,
                       new_min=args.new_min, abs_big=args.abs_big,
                       conn_min=args.conn_min, new_rate_min=args.new_rate_min)

    print(f"== 巡逻检查（{iso(now)}）｜模型：{provider['label']} ==")
    print(f"   预筛候选：{len(cands)} 条")
    if not cands:
        return 0

    state = load_state()
    cases: list[dict] = []
    for i, cand in enumerate(cands, 1):
        proc = cand["process"]
        last = state["cases"].get(proc, {}).get("last_ts", 0)
        if not args.case and now - last < args.cooldown * 3600:
            print(f"   [{i}/{len(cands)}] {proc} —— 冷却中（上次取证 {iso(last)}），跳过")
            continue
        print(f"   [{i}/{len(cands)}] {proc}  {human(cand['total'])}  → 取证中…")
        try:
            case = (mock_case(cand) if provider["name"] == "mock"
                    else run_case(cand, provider, args.max_turns, timeout=args.timeout))
        except Exception as exc:  # noqa: BLE001 - 单个候选失败不拖垮整轮巡逻
            print(f"         取证失败：{type(exc).__name__}: {exc}")
            continue
        path = save_report(case)
        state["cases"][proc] = {"last_ts": case["ts"], "verdict": case["verdict"]}
        cases.append(case)
        print(f"         结论：{case['verdict']}（{case['elapsed']}s，{len(case['trace'])} 次工具调用）")
        print(f"         报告：{path}")
        if not args.no_alert and notify_case(case, path):
            print("         ⚠️ 已推送告警（可疑）")
    save_state(state)
    return 0


def main() -> int:
    # 计划任务用 pythonw（无控制台）跑时 sys.stdout/stderr 是 None：任何 print 都会抛
    # AttributeError（表现为"静默死亡、死因不可考"——M-38/E-24 同族）。先把输出落到日志。
    if sys.stdout is None or sys.stderr is None:
        try:
            RUN_DIR.mkdir(parents=True, exist_ok=True)
            _log = open(CRON_LOG, "a", encoding="utf-8")
            sys.stdout = _log
            sys.stderr = _log
        except Exception:
            pass

    ap = argparse.ArgumentParser(description="patrol —— 本机流量巡检（预筛 + Agent 取证）")
    ap.add_argument("--version", action="version", version=f"patrol {__version__}")
    ap.add_argument("--check", action="store_true", help="完整巡逻：预筛 → 取证 → 报告")
    ap.add_argument("--case", type=str, metavar="PROCESS", help="手动指定进程作为候选（跳过预筛）")
    ap.add_argument("--replay", type=int, metavar="HOURS", help="回测最近 N 小时（只做预筛）")
    ap.add_argument("--mock", action="store_true", help="用 mock 取证（不联网，验证链路）")
    ap.add_argument("--no-alert", action="store_true", help="不推送告警（只落报告）")
    ap.add_argument("--test-alert", action="store_true", help="弹一次测试告警（验证通道）")
    ap.add_argument("--cooldown", type=float, default=COOLDOWN_HOURS, help="同进程冷却小时数（默认 6）")
    ap.add_argument("--max-turns", type=int, default=MAX_TURNS, help="取证轮数上限（默认 6）")
    ap.add_argument("--timeout", type=float, default=120.0,
                    help="单次模型请求超时（秒，默认 120）——本地小模型建议 300+")
    ap.add_argument("--json", type=str, help="（预筛模式）候选落盘路径")
    ap.add_argument("--factor", type=float, default=FACTOR)
    ap.add_argument("--min-cur", type=int, default=MIN_CUR, help="量级突破绝对底线（字节）")
    ap.add_argument("--new-min", type=int, default=NEW_MIN, help="新面孔流量门槛（字节）")
    ap.add_argument("--abs", type=int, default=ABS_BIG, dest="abs_big", help="绝对巨量兜底（字节）")
    ap.add_argument("--conn-min", type=int, default=CONN_MIN, dest="conn_min",
                    help="连接数绝对底线（条；信号 D 冷启动期用）")
    ap.add_argument("--new-rate-min", type=float, default=NEW_MIN_RATE, dest="new_rate_min",
                    help="新建连接速率绝对底线（条/秒；信号 D 冷启动期用）")
    # provider 调试覆盖（一般不需要——默认读 flowwatch 的 assistant_config.json）
    ap.add_argument("--provider", type=str, default="", help="覆盖 provider（openai/ollama/mock）")
    ap.add_argument("--base-url", type=str, default="", help="覆盖 base_url")
    ap.add_argument("--api-key", type=str, default="", help="覆盖 api_key")
    ap.add_argument("--model", type=str, default="", help="覆盖模型名")
    args = ap.parse_args()

    kw = dict(factor=args.factor, min_cur=args.min_cur,
              new_min=args.new_min, abs_big=args.abs_big)

    if args.test_alert:
        alert("流量巡检测试",
              "这是一条测试告警：桌面文件 + Catrace 小窗（未装/未启用则退回置顶弹窗）通道正常。\n\n"
              "实际告警只在取证结论为「可疑」时推送（正常/无法判断只落报告）。")
        print("测试告警已触发（桌面文件已写入）。")
        return 0

    if args.check or args.case:
        return do_check(args)

    if args.replay:
        records = replay(args.replay, **kw)
        days = max(args.replay / 24, 1e-9)
        print(f"== 回测：最近 {args.replay} 小时（每小时一个检查点）==")
        print(f"   参数 factor={args.factor}  min_cur={human(args.min_cur)}  "
              f"new_min={human(args.new_min)}  abs_big={human(args.abs_big)}")
        print(f"   共 {len(records)} 条候选，平均 {len(records) / days:.1f} 条/天")
        if records:
            by_proc = Counter(c["process"] for _t, c in records)
            by_kind = Counter(s["kind"] for _t, c in records for s in c["signals"])
            print(f"   信号分布：{dict(by_kind)}")
            print("   按进程 Top 10：")
            for proc, cnt in by_proc.most_common(10):
                print(f"     {proc[:40]:40s} {cnt:4d} 次")
            print("   候选明细（新→旧，前 40 条）：")
            for t, c in reversed(records):
                when = datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
                why = "；".join(f"{s['kind']}·{s['why']}" for s in c["signals"])
                print(f"     {when}  {c['process'][:36]:36s} {human(c['total']):>10s} | {why}")
        payload = [{"ts": t, **c} for t, c in records]
    else:
        now = int(time.time())
        cands = screen(now, **kw)
        print(f"== 当前时刻预筛（{iso(now)}）==")
        if not cands:
            print("   无候选：窗口内没有任何进程突破自身历史。")
        for c in cands:
            why = "；".join(f"{s['kind']}·{s['why']}" for s in c["signals"])
            print(f"   {c['process'][:40]:40s} {human(c['total']):>10s} | {why}")
        payload = cands

    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"（已落盘：{args.json}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
