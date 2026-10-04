#!/usr/bin/env python3
"""patrol · 第一段：预筛——发现「值得看一眼」的候选。

确定性、只读历史库、零模型成本。两路信号融合：
  · 字节视角（抓包字节数）：A 量级突破 / B 新面孔 / C 绝对巨量；
  · 连接视角（系统连接表快照）：D 连接数突破 / 新建风暴。

为什么不用 events 表的 spike/appear 直接当候选（2026-10-03 侦察结论）：
本机 7 天真实数据里 spike 常客是 Steam++（367 次）、CodeBuddy（205 次）、
Tabbit（158 次）——单桶 3× 暴涨是常驻进程的日常波动；vanish 是 2199 条/天的纯噪声。
真正有区分度的是「进程突破自身历史」与「连接形态突变」。

信号只是"值得看一眼"，不是结论——是否异常由 forensics 取证回答。
"""
from __future__ import annotations

import sqlite3
import time

from common import (
    ABS_BIG, CONN_DAYS, CONN_FACTOR, CONN_HIST_MIN_BUCKETS, CONN_MIN, FACTOR,
    HIST_DAYS, MIN_CUR, NEW_FACTOR, NEW_MIN, NEW_MIN_RATE, WINDOW,
    connect, human,
)


# ---- 字节视角查询（数据源：buckets 时间桶）----

def hourly_by_process(conn: sqlite3.Connection, since: int, until: int) -> dict[str, dict[int, int]]:
    """[since, until) 内按自然小时聚合：{进程名: {小时起始秒: 合计字节}}。"""
    rows = conn.execute(
        """SELECT process, (bucket_ts / 3600) * 3600 AS hr, SUM(in_bytes + out_bytes) AS total
           FROM buckets
           WHERE pid > 0 AND bucket_ts >= ? AND bucket_ts < ?
           GROUP BY process, hr""",
        (since, until),
    ).fetchall()
    out: dict[str, dict[int, int]] = {}
    for proc, hr, total in rows:
        out.setdefault(proc, {})[hr] = total
    return out


def window_by_process(conn: sqlite3.Connection, since: int, until: int) -> dict[str, int]:
    """[since, until) 的滑动窗口合计：{进程名: 字节}。"""
    rows = conn.execute(
        """SELECT process, SUM(in_bytes + out_bytes) FROM buckets
           WHERE pid > 0 AND bucket_ts >= ? AND bucket_ts < ?
           GROUP BY process""",
        (since, until),
    ).fetchall()
    return {proc: total for proc, total in rows}


def first_seen_by_process(conn: sqlite3.Connection) -> dict[str, int]:
    """每个进程名在全库中最早出现的桶时刻。"""
    rows = conn.execute(
        "SELECT process, MIN(bucket_ts) FROM buckets WHERE pid > 0 GROUP BY process"
    ).fetchall()
    return dict(rows)


# ---- 连接视角查询（数据源：conn_buckets，flowwatch 的 localconn 快照落桶）----

def conn_peaks_by_process(conn: sqlite3.Connection, since: int, until: int) -> dict[str, tuple[int, float]]:
    """[since, until) 内每进程的连接峰值：{进程名: (conns_peak, new_per_sec_peak)}。"""
    rows = conn.execute(
        """SELECT process, MAX(conns_peak), MAX(new_per_sec_peak) FROM conn_buckets
           WHERE bucket_ts >= ? AND bucket_ts < ? GROUP BY process""",
        (since, until),
    ).fetchall()
    return {proc: (int(c or 0), float(n or 0.0)) for proc, c, n in rows}


def conn_series_by_process(conn: sqlite3.Connection, since: int, until: int) -> dict[str, dict[str, list]]:
    """[since, until) 内每进程的连接峰值序列（供相对判定）：{进程名: {"conns": [...], "new": [...]}}。"""
    rows = conn.execute(
        """SELECT process, conns_peak, new_per_sec_peak FROM conn_buckets
           WHERE bucket_ts >= ? AND bucket_ts < ?""",
        (since, until),
    ).fetchall()
    out: dict[str, dict[str, list]] = {}
    for proc, c, n in rows:
        slot = out.setdefault(proc, {"conns": [], "new": []})
        slot["conns"].append(int(c or 0))
        slot["new"].append(float(n or 0.0))
    return out


# ---- 信号判定（纯函数，单测与回测共用）----

def judge_conns(proc: str, conns_peak: int, new_peak: float,
                hist: dict[str, list] | None,
                conn_factor: float = CONN_FACTOR, conn_min: int = CONN_MIN,
                new_factor: float = NEW_FACTOR, new_rate_min: float = NEW_MIN_RATE,
                hist_min_buckets: int = CONN_HIST_MIN_BUCKETS) -> list[tuple[str, str]]:
    """连接视角信号（纯函数）。冷启动保护：历史样本不足时只用绝对底线、不做倍数判定。

    语义：连接数异常指向「连接风暴」类问题（应用疯狂重试/扫端口/连接泄漏），
    这类问题在字节视角可能很小甚至看不见——两个视角互补。
    """
    signals: list[tuple[str, str]] = []
    if conns_peak >= conn_min:
        if hist and len(hist.get("conns", [])) >= hist_min_buckets:
            hmax = max(hist["conns"])
            if hmax > 0 and conns_peak >= hmax * conn_factor:
                signals.append(("连接数突破",
                                f"峰值 {conns_peak} 条 vs 历史峰值 {hmax} 条（{conns_peak / hmax:.1f}×）"))
        else:
            signals.append(("连接数异常", f"峰值 {conns_peak} 条 ≥ 绝对底线 {conn_min}（冷启动期，无历史基线）"))
    if new_peak >= new_rate_min:
        if hist and len(hist.get("new", [])) >= hist_min_buckets:
            hmax = max(hist["new"])
            if hmax > 0 and new_peak >= hmax * new_factor:
                signals.append(("新建风暴",
                                f"{new_peak:.1f} 条/秒 vs 历史峰值 {hmax:.1f} 条/秒（{new_peak / hmax:.1f}×）"))
        else:
            signals.append(("新建风暴", f"{new_peak:.1f} 条/秒 ≥ 绝对底线 {new_rate_min}（冷启动期）"))
    return signals


def judge(proc: str, total: int, hist: list[int], first_seen: int | None,
          w0: int, factor: float, min_cur: int, new_min: int,
          abs_big: int) -> list[tuple[str, str]]:
    """字节视角信号（纯函数，单测与回测共用）。

    hist: 该进程在 [w0 - HIST_DAYS, w0) 内的小时值列表（不含当前窗口）。
    新面孔的保守口径：仅当近 7 天无任何记录、且进程名在全库中都 ≥ w0 才首现时命中
    （休眠很久后重新活跃的老进程本轮不报——MVP 保守取舍，回测再决定要不要放宽）。
    """
    signals: list[tuple[str, str]] = []
    if total >= abs_big:
        signals.append(("绝对巨量", f"{human(total)}/小时 ≥ 兜底线 {human(abs_big)}"))
    if hist:
        mx = max(hist)
        if mx > 0 and total >= max(int(mx * factor), min_cur):
            signals.append(("量级突破",
                            f"{human(total)}/小时 vs 历史最大 {human(mx)}"
                            f"（{total / mx:.1f}×，{len(hist)} 个基线小时）"))
    elif total >= new_min and (first_seen is None or first_seen >= w0):
        signals.append(("新面孔", f"{human(total)}/小时，该进程名此前从未出现过"))
    return signals


# ---- 预筛主入口 ----

def screen(now: int, factor: float = FACTOR, min_cur: int = MIN_CUR,
           new_min: int = NEW_MIN, abs_big: int = ABS_BIG,
           conn_factor: float = CONN_FACTOR, conn_min: int = CONN_MIN,
           new_factor: float = NEW_FACTOR, new_rate_min: float = NEW_MIN_RATE) -> list[dict]:
    """对时刻 now 做一次预筛（只读，滑动 60 分钟窗口）。返回按流量降序的候选。

    两路信号融合：字节视角（A/B/C，抓包）+ 连接视角（D，系统连接表快照）。
    仅连接命中的进程同样成为候选——「连接风暴但字节不显」是真实存在的形态
    （重试风暴的字节可能很小；流量被代理代收时字节会记在别人头上）。
    """
    conn = connect()
    try:
        w0 = now - WINDOW
        hist = hourly_by_process(conn, now - HIST_DAYS * 86400 - WINDOW, w0)
        cur = window_by_process(conn, w0, now)
        first_seen = first_seen_by_process(conn)
        cands: dict[str, dict] = {}
        for proc, total in cur.items():
            sigs = judge(proc, total, list(hist.get(proc, {}).values()),
                         first_seen.get(proc), w0, factor, min_cur, new_min, abs_big)
            if sigs:
                cands[proc] = {"process": proc, "total": total,
                               "signals": [{"kind": k, "why": w} for k, w in sigs]}
        # 连接视角：表可能不存在（老库 / 未接线的 server）——如实降级为无该路信号
        try:
            cnow = conn_peaks_by_process(conn, w0, now)
            chist = conn_series_by_process(conn, now - CONN_DAYS * 86400 - WINDOW, w0)
        except sqlite3.OperationalError:
            cnow, chist = {}, {}
        for proc, (c_peak, n_peak) in cnow.items():
            sigs_c = judge_conns(proc, c_peak, n_peak, chist.get(proc),
                                 conn_factor, conn_min, new_factor, new_rate_min)
            if sigs_c:
                slot = cands.get(proc)
                if slot is None:
                    slot = cands[proc] = {"process": proc, "total": cur.get(proc, 0), "signals": []}
                slot["signals"].extend({"kind": k, "why": w} for k, w in sigs_c)
        return sorted(cands.values(), key=lambda c: -c["total"])
    finally:
        conn.close()


def replay(hours: int, factor: float = FACTOR, min_cur: int = MIN_CUR,
           new_min: int = NEW_MIN, abs_big: int = ABS_BIG) -> list[tuple[int, dict]]:
    """回测：对最近 hours 小时内的每个整点做一次预筛（内存聚合，秒级完成）。

    与实时模式的口径差异：这里的「当前窗口」对齐到自然小时（[t-1h, t)），
    而非滑动 60 分钟——回测目的是评估候选率量级，可接受；实时模式仍是滑动窗口。
    连接视角暂不参与回测（conn_buckets 自 2026-10-04 才有数据，冷启动期无基线）。
    """
    conn = connect()
    try:
        now = int(time.time())
        stop = (now // 3600) * 3600                    # 最后一个完整小时
        hours_map = hourly_by_process(conn, 0, stop)   # {进程: {hr: 值}}
        first_seen = first_seen_by_process(conn)
    finally:
        conn.close()

    hist_sec = HIST_DAYS * 86400
    records: list[tuple[int, dict]] = []
    t = stop - hours * 3600
    while t <= stop:
        w0, w1 = t - WINDOW, t
        for proc, hours_of in hours_map.items():
            total = hours_of.get(w0, 0)                # 对齐：一个自然小时
            if total <= 0:
                continue
            hist_vals = [v for hr, v in hours_of.items()
                         if w0 - hist_sec <= hr < w0]
            sigs = judge(proc, total, hist_vals, first_seen.get(proc),
                         w0, factor, min_cur, new_min, abs_big)
            if sigs:
                records.append((t, {"process": proc, "total": total,
                                    "signals": [{"kind": k, "why": w} for k, w in sigs]}))
        t += 3600
    return records
