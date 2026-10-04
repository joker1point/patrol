#!/usr/bin/env python3
"""patrol · 公共层：路径定位、数据源连接、格式化与共享常量。

数据契约（与 flowwatch 的耦合边界——只消费它的数据，不 import 它的代码）：
  · flowwatch 的历史库 `history.db`（只读）——监控数据来源；
  · flowwatch 助手的 `assistant_config.json`——模型凭据（同一台机器共用一份）。
两者都可用环境变量覆盖：`PATROL_DB` / `PATROL_FLOWWATCH_DIR` / `PATROL_CONFIG`。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

__version__ = "0.1.0"     # 与 git tag / README badge 同源（发版时三处一起改）

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / "_run"
REPORTS_DIR = RUN_DIR / "reports"
STATE_PATH = RUN_DIR / "state.json"
CRON_LOG = RUN_DIR / "patrol_cron.log"


# ---- 数据源定位（默认与 flowwatch 在工作区相邻）----

def flowwatch_dir() -> Path:
    override = os.environ.get("PATROL_FLOWWATCH_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return ROOT.parent / "flowwatch"


def db_path() -> Path:
    override = os.environ.get("PATROL_DB")
    return Path(override).expanduser() if override else flowwatch_dir() / "history.db"


def config_path() -> Path:
    """模型配置：复用 flowwatch 助手的 assistant_config.json（同一台机器、同一套凭据）。"""
    override = os.environ.get("PATROL_CONFIG")
    return Path(override).expanduser() if override else flowwatch_dir() / "assistant_config.json"


def connect() -> sqlite3.Connection:
    """只读连接历史库。库不存在时给出可操作的错误（而不是一个 sqlite 异常）。"""
    path = db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"找不到 flowwatch 历史库：{path}\n"
            "patrol 依赖 flowwatch 的采集数据运行——请先启动 flowwatch，"
            "或用 PATROL_DB / PATROL_FLOWWATCH_DIR 指向实际位置。")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n} B"


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# ---- 预筛参数（2026-10-03 用本机 7 天真实数据定标；回测可调）----
WINDOW = 3600                          # 考察窗口：最近 60 分钟（秒）
HIST_DAYS = 7                          # 基线：之前 7 天的小时值
FACTOR = 2.0                           # 量级突破：≥ 历史小时最大值 × factor
MIN_CUR = 200 * 1024 * 1024            # 量级突破的绝对底线：200 MB（避免小流量噪声）
NEW_MIN = 60 * 1024 * 1024             # 新面孔：≥ 60 MB
ABS_BIG = 5 * 1024 ** 3                # 绝对巨量兜底：5 GB

# ---- 连接视角参数（信号 D；conn_buckets 由 flowwatch 自 2026-10-04 起采集）----
CONN_DAYS = 7                          # 连接基线窗口：之前 7 天
CONN_FACTOR = 2.0                      # 连接数突破：≥ 自身历史峰值 × factor
CONN_MIN = 120                         # …且 ≥ 绝对底线（空闲全机最高约 10 条；浏览器活跃可达几十）
NEW_FACTOR = 3.0                       # 新建风暴：新建速率 ≥ 自身历史峰值 × factor
NEW_MIN_RATE = 12.0                    # …且 ≥ 绝对底线（/秒；实测日常 0~1.3）
CONN_HIST_MIN_BUCKETS = 3              # 历史样本少于此数：只用绝对底线（冷启动保护）

# ---- 取证参数 ----
MAX_TURNS = 6                          # 一个候选最多几轮模型往返（含工具轮）
COOLDOWN_HOURS = 6.0                   # 同进程冷却：多久内不重复取证
