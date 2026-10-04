"""patrol 预筛信号单测（纯函数，不碰数据库，零依赖）。

覆盖：judge（字节信号 A/B/C）与 judge_conns（连接信号 D）的边界——
包含冷启动保护、绝对底线、新面孔的保守口径（老进程复出不算新面孔）。

运行：python tests/test_screen.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from screen import judge, judge_conns  # noqa: E402

W0 = 1_700_000_000
MB = 1024 ** 2
GB = 1024 ** 3


# ---- 字节信号 judge ----

def test_scale_break():
    """量级突破：≥ max(历史最大 × factor, 绝对底线)。"""
    sigs = judge("a.exe", 3 * GB, [512 * MB] * 10, None, W0, 2.0, 200 * MB, 60 * MB, 5 * GB)
    assert [k for k, _ in sigs] == ["量级突破"], sigs


def test_below_floor_no_signal():
    """倍数够但低于绝对底线 → 不报。"""
    sigs = judge("a.exe", 10 * MB, [1 * MB] * 10, None, W0, 2.0, 200 * MB, 60 * MB, 5 * GB)
    assert sigs == [], sigs


def test_abs_big_backstop():
    """绝对巨量不依赖历史。"""
    sigs = judge("a.exe", 6 * GB, [], None, W0, 2.0, 200 * MB, 60 * MB, 5 * GB)
    assert "绝对巨量" in [k for k, _ in sigs], sigs


def test_new_face():
    """新面孔：无历史 + 全库首见在窗口内 + ≥ 门槛。"""
    sigs = judge("new.exe", 100 * MB, [], W0 + 60, W0, 2.0, 200 * MB, 60 * MB, 5 * GB)
    assert [k for k, _ in sigs] == ["新面孔"], sigs


def test_old_process_reborn_not_new_face():
    """老进程（首现早于窗口）近 7 天无记录 → 保守不报新面孔。"""
    sigs = judge("old.exe", 100 * MB, [], W0 - 86400, W0, 2.0, 200 * MB, 60 * MB, 5 * GB)
    assert sigs == [], sigs


# ---- 连接信号 judge_conns ----

def test_conns_cold_start_floor():
    """冷启动（无历史）：只用绝对底线。"""
    sigs = judge_conns("a.exe", 150, 0.0, None)
    assert [k for k, _ in sigs] == ["连接数异常"], sigs


def test_conns_relative_break():
    """有历史：倍数判定（300 vs 历史峰值 12）。"""
    hist = {"conns": [10, 12, 8], "new": [0.5, 0.2, 0.3]}
    sigs = judge_conns("a.exe", 300, 0.4, hist)
    assert [k for k, _ in sigs] == ["连接数突破"], sigs


def test_conns_new_storm():
    """新建风暴：新建速率 ≥ max(历史峰值 × factor, 绝对底线)。"""
    hist = {"conns": [10, 12, 8], "new": [0.5, 0.2, 0.3]}
    sigs = judge_conns("a.exe", 20, 40.0, hist)
    assert [k for k, _ in sigs] == ["新建风暴"], sigs


def test_conns_normal_no_signal():
    """正常波动：低于底线且不突破历史 → 无信号。"""
    hist = {"conns": [10, 12, 8], "new": [0.5, 0.2, 0.3]}
    assert judge_conns("a.exe", 9, 0.4, hist) == []


def test_conns_hist_too_short_cold_start():
    """历史样本不足（< min_buckets）→ 退回冷启动口径（只用绝对底线）。"""
    hist = {"conns": [10], "new": [0.5]}
    sigs = judge_conns("a.exe", 300, 0.0, hist)
    assert [k for k, _ in sigs] == ["连接数异常"], sigs   # 300 ≥ 120 底线，但不做倍数判定


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {fn.__name__}: {exc}")
    print(f"共 {len(fns)} 项，失败 {failed}")
    sys.exit(1 if failed else 0)
