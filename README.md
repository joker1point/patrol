# patrol

![version](https://img.shields.io/badge/version-0.1.0-blue)
![license](https://img.shields.io/badge/license-MIT-green)

> 让 AI Agent 帮你盯着本机流量与进程：**自动发现异常、多步取证、只对可疑的告警**。
> portwatch 回答「此刻谁占着端口」，flowwatch 回答「历史上谁在用带宽」——patrol 回答「**有没有不正常**」。
>
> *Let an AI agent watch your machine's traffic and processes — auto-detect anomalies,
> multi-step forensics, alert only when suspicious.*

三个产品是一条线：**观测 → 留档 → 守护**。patrol 是第三层——它不替你下结论，
它把「值得看一眼的进程」挑出来，让 Agent 取证，把证据链放在你面前。

## 它做什么

计划任务每 10 分钟跑一轮 `--check`：

```
① 预筛（确定性，零模型成本，只读 flowwatch 的 history.db）
   字节视角：突破自身 7 天历史的进程     +     连接视角：连接数 / 新建速率异常
        │
        ▼  候选 ≈ 0.9 条/天（7 天回测实测）
② 取证（只在候选上触发，轻量 Agent 循环 ≤6 轮）
   5 个只读工具：进程身份（exe 版本资源）/ 流量历史 / 变化事件 / 连接形态 / 域名旁证
        │
        ▼  结论三选一：正常 / 可疑 / 无法判断（每条依据必须带数字）
③ 输出
   报告落档 _run/reports/ ＋ 只有「可疑」才告警（Catrace 小窗 → 置顶弹窗 → 桌面文件）
```

## 为什么是「两段式」

一开始想直接用 flowwatch 事件表里的 `spike`/`appear` 当候选——**实测把它否了**：
本机 7 天里 spike 常客是 Steam++（367 次）、CodeBuddy（205 次）、Tabbit（158 次），
单桶 3× 暴涨全是常驻进程的日常波动（~100 条/天），`vanish` 更是 2199 条/天的纯噪声。

**真正有区分度的是「进程突破自身历史」**——异常的形态是"某个进程干出了它从没干过的事"。
但数值比较不该交给 LLM：规则便宜、确定、可回测；LLM 只负责规则的短板——开放域的
「这是谁 / 为什么 / 正不正常」。两层各干各的擅长的事，也各自可验证。

## 信号栈

| # | 信号 | 视角 | 判定（默认参数） |
|---|---|---|---|
| A | 量级突破 | 字节 | 近 60 分钟 ≥ max(自身 7 天小时峰值 × 2, 200 MB) |
| B | 新面孔 | 字节 | 进程名全库首现 + ≥ 60 MB |
| C | 绝对巨量 | 字节 | 近 60 分钟 ≥ 5 GB（兜底，不看历史） |
| D | 连接数突破 / 新建风暴 | 连接 | ≥ max(自身 7 天峰值 × 2, 120 条) / (× 3, 12 条/秒) |

连接视角（D）是独立于抓包的第二数据源：**流量被代理/加速软件代收时，字节记在代收者
头上，而连接表能看出进程自己的形态**（2026-10-04 实测：233 MB 下载被 Steam++ 全量代收，
三层可见性 = 字节总量 ✓ / 进程归代收者 ✗ / 域名只剩 IP 字面量 ✗）。冷启动期 D 用绝对
底线；数据积累后自动升级为相对历史判定。

## 证据纪律（都是实测踩出来的）

| 层 | 机制 | 来源 |
|---|---|---|
| 1 | 首轮协议层 `tool_choice="required"`（不认此字段的 provider 自动退回 auto） | 模型首轮零工具调用、空转作答 |
| 2 | 零工具调用时提醒补查一轮 | 同上（"没查"与"没查到"必须分开） |
| 3 | 工具自带**基准数据**：spike 7 天频度、历史库记录起点 | 首版把 Steam++ 判「可疑」——它不知道 15 次 spike 是日常节奏 |
| 4 | 系统提示词第 7 条：「多数候选是正常的，不要为了显得有用而拔高结论」 | 同上（防表演性警觉） |

结论只能是 正常 / 可疑 / 无法判断；工具取不到数据时必须「无法判断」，不许猜。

## 实测记录

| 验证 | 手段（可复现） | 结果 |
|---|---|---|
| 预筛候选率 | `python patrol.py --replay 168` | 7 天 6 条候选（0.9 条/天），全部可解释 |
| 误判修正闭环 | 同一 case 前后对比（qwen-plus） | 首判「可疑」→ 补基准数据 + 纪律 → 重跑「正常」，依据链健康 |
| 连接视角端到端 | 受控连接风暴（150 条到本机端口）→ `--check` | 仅连接命中（字节 0 B）→ 自动取证 4 次工具调用 → 保守「无法判断」 |
| 代收盲区 | 三次对照下载实验 | 83.9 / 320 / 233.5 MB 下载 → 库里分别归给 Steam++ 76.8 / 372 / 350.9 MB，curl 只剩零头 |
| 告警通道 | `python patrol.py --test-alert` | 桌面文件 ✓、弹窗 ✓、Catrace 未装时如实降级 |

## 边界（诚实标注，不假装已解决）

- **依赖 flowwatch 在采集**：patrol 只消费它的 `history.db`（只读，数据契约），不重复造采集；
- **代收型盲区**：字节被代理/加速软件吃掉时，字节视角看不见真实发起方。连接视角部分补偿
  （能看见"谁在发起连接"），完整解法（ETW 连接事件 ↔ 代收者字节交叉印证）是后续方向；
- **连接信号阈值**：目前冷启动期用绝对底线（120 条 / 12 条每秒），跑几天后升级为相对历史；
- **只告警、不处置**：不断连接、不封端口——判断权与处置权都在人手上；
- **只读承诺**：取证工具全部只读；本程序的写面只有自己的运行时数据（报告 / 冷却状态 / 桌面告警文件）。

## 用法

```bash
python patrol.py                          # 只做预筛（dry-run，零模型成本）
python patrol.py --replay 168             # 回测最近 168 小时
python patrol.py --check                  # 完整巡逻：预筛 → 取证 → 报告
python patrol.py --check --mock           # mock 取证（不联网，仍真跑工具）
python patrol.py --check --case xxx.exe   # 手动指定候选（测试取证链路）
python patrol.py --test-alert             # 验证告警通道
python tests/test_screen.py               # 预筛信号单测（10 项，零依赖）
```

计划任务（每 10 分钟，注册后可复跑）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/register_task.ps1
Get-ScheduledTaskInfo -TaskName patrol       # 状态
Disable-ScheduledTask -TaskName patrol       # 暂停
Unregister-ScheduledTask -TaskName patrol    # 卸载
```

模型配置默认复用 flowwatch 助手的 `assistant_config.json`（同机同凭据）；
环境变量可覆盖：`PATROL_FLOWWATCH_DIR` / `PATROL_DB` / `PATROL_CONFIG` /
`PATROL_PROVIDER` / `PATROL_BASE_URL` / `PATROL_API_KEY` / `PATROL_MODEL`。

## 结构

| 文件 | 职责 |
|---|---|
| `patrol.py` | CLI 入口（巡逻编排 / 回测输出 / 无控制台保护） |
| `screen.py` | 第一段：预筛（信号 A/B/C/D，纯函数可单测） |
| `forensics.py` | 第二段：取证（provider / 5 只读工具 / Agent 循环 / mock） |
| `report.py` | 报告落档 + 冷却状态 + 告警三通道 |
| `common.py` | 路径与数据契约、共享常量 |
| `tests/` | 预筛信号单测 |
| `scripts/` | 计划任务注册（含注册后校验） |
| `_run/` | 运行时数据：`reports/` 报告、`state.json` 冷却、`patrol_cron.log` 日志 |

依赖：Python 3.10+ / `psutil`（进程身份）。零框架——LLM 调用只用标准库 `urllib`。
