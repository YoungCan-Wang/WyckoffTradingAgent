# A 股主漏斗执行流程

> 本文描述 A 股 Wyckoff 主漏斗从 GitHub Actions 触发到 Supabase 写库、跨日反馈闭环的完整执行链路。
> **实盘操作口径**见 [`OPERATOR_PLAYBOOK.md`](OPERATOR_PLAYBOOK.md)。策略逻辑见 [`../README_STRATEGY.md`](../README_STRATEGY.md)，架构见 [`ARCHITECTURE.md`](ARCHITECTURE.md)。

**主入口**：`.github/workflows/wyckoff_funnel.yml` → `scripts/daily_job.py`（周日到周四 **17:17** 北京时间；周日正常为周一实盘准备候选，仅在次日不是 A 股交易日时跳过）

---

## 一、系统全景：上下游关系

```mermaid
flowchart TB
    subgraph UPSTREAM["⬆️ 上游（漏斗运行前已存在）"]
        U1["GitHub Actions 触发<br/>wyckoff_funnel.yml<br/>周日到周四 17:17 北京<br/>周日为周一实盘准备候选"]
        U2["环境变量 / Secrets<br/>TICKFLOW / TUSHARE / LLM / Supabase / IM"]
        U3["本地元数据<br/>行业映射 / 概念映射 / 股票池"]
        U4["前日反馈闭环<br/>signal_health_daily<br/>signal_registry"]
        U5["前日盘前风控<br/>premarket_risk → market_signal_daily"]
        U6["前日漏斗产出<br/>signal_pending 待确认信号"]
        U7["外部观察名单<br/>profile / env / symbols_file"]
    end

    subgraph CORE["🔬 核心：daily_job.py"]
        S2["Step2 Wyckoff Funnel<br/>workflows/wyckoff_funnel.py"]
        S25["Step2.5 信号确认<br/>pending → confirmed"]
        S26["Step2.6 推荐写库<br/>recommendation_tracking"]
        S27["Step2.7 起跳板/候选影子评分"]
        S3["Step3 批量 AI 研报<br/>workflows/step3_batch_report.py"]
        S4["Step4 私人 OMS 再平衡<br/>workflows/step4_rebalancer.py"]
    end

    subgraph DOWNSTREAM["⬇️ 下游（漏斗运行后消费）"]
        D1["23:30 signal_feedback_job<br/>计算 outcomes / health / registry"]
        D2["次日 08:20 premarket_risk<br/>Step4 买入门控"]
        D3["次日尾盘 tail_buy_intraday<br/>读 signal_pending 尾盘买入"]
        D4["Web / CLI / MCP<br/>chat-agent 工具调用"]
        D5["回测 backtest_runner<br/>读 funnel_snapshots"]
        D6["recommendation_tracking_reprice<br/>复盘重定价"]
        D7["飞书 / 企微 / 钉钉 / Telegram"]
    end

    U1 --> CORE
    U2 --> CORE
    U3 --> S2
    U4 --> S2
    U5 --> S4
    U6 --> S25
    U7 --> S2

    S2 --> S25 --> S26 --> S27 --> S3 --> S4

    S2 --> D7
    S3 --> D7
    S4 --> D7

    S2 --> D1
    S3 --> D1
    S4 --> D2
    S25 --> D3
    S26 --> D6
    S2 --> D5
    CORE --> D4
```

---

## 二、主入口：`daily_job.py` 完整执行链

**触发**：`.github/workflows/wyckoff_funnel.yml` → `python scripts/daily_job.py`

```mermaid
flowchart TD
    START(["GitHub Actions 17:17<br/>wyckoff_funnel.yml"]) --> CHECK1{"配置校验<br/>LLM Key / Model"}
    CHECK1 -->|缺失| FAIL1["exit 1"]
    CHECK1 -->|通过| CHECK2{"次日交易日判定<br/>明日是否 A 股交易日?"}
    CHECK2 -->|否| SKIP["IM 通知跳过<br/>exit 0"]
    CHECK2 -->|是| STEP2

    STEP2["Step2: run_funnel()<br/>wyckoff_funnel.py"] --> P1["写 market_signal_daily<br/>大盘水温 regime"]
    STEP2 --> P2["写 theme_radar_snapshot<br/>主题雷达"]
    STEP2 --> S25

    S25["Step2.5: run_step2_5()<br/>signal_pending 确认"] --> S26
    S26["Step2.6: prepare_recommendation_payload<br/>→ recommendation_tracking"] --> S27
    S27["Step2.7: score_springboard_abc<br/>起跳板/候选影子评分"] --> S3

    S3["Step3: run_step3()<br/>批量 AI 研报"] --> MARK["mark_ai_recommendations<br/>标记起跳板"]
    MARK --> OBS["写 signal_observations<br/>L4 观察样本"]

    S3 --> S4CHK{"Step4 启用?<br/>SUPABASE_USER_ID + TG"}
    S4CHK -->|跳过| SUM
    S4CHK -->|执行| S4

    S4["Step4: run_step4()<br/>持仓决断 + Telegram"] --> SUM
    SUM["阶段汇总日志<br/>upload artifacts"] --> END(["exit 0/1"])

    STEP2 -->|异常| BLOCK["阻断型失败 exit 1"]
    OBS -->|失败| BLOCK
```

### 阶段与代码映射

| 阶段 | 入口 | 核心模块 |
|------|------|----------|
| 调度 | `wyckoff_funnel.yml` | GitHub Actions |
| 编排 | `scripts/daily_job.py` | 主流程 |
| Step2 | `workflows/wyckoff_funnel.py` | `core/wyckoff_engine.py` |
| Step3 | `workflows/step3_batch_report.py` | `tools/report_builder.py` |
| Step4 | `workflows/step4_rebalancer.py` | `core/holding_diagnostic.py` / `core/wyckoff_engine.py` |

---

## 三、Step2 漏斗内部：主线发现 + 多车道详细流程

**核心函数**：`run_funnel_job()` → `core/wyckoff_engine.py`

```mermaid
flowchart TD
    subgraph PREP["阶段 0：数据准备"]
        P0["解析交易日窗口<br/>320 个交易日"]
        P1["加载股票池<br/>主板 + 创业板 + 科创板 → 去 ST"]
        P2["加载元数据<br/>行业 / 概念 / 概念热度 / 市值 / 名称"]
        P3["TickFlow 财务指标<br/>financial_map"]
        P4["拉取基准指数<br/>000001 + 小盘指数"]
        P5["fetch_all_ohlcv 批量拉 K 线<br/>TickFlow → tushare → akshare → baostock → efinance"]
        P6["dump funnel_snapshots<br/>离线快照"]
        P7["ETF 增强扫描<br/>_run_etf_enhancement"]
        P8["加载 external_seeds<br/>追加到观察池"]
    end

    subgraph GATE["阶段 0.5：大盘总闸"]
        G1["calc_market_breadth<br/>市场广度"]
        G2["analyze_benchmark_and_tune_cfg<br/>regime 判定"]
        G3{"水温 regime"}
        G3 -->|NEUTRAL| T1["主战场 mainline_active<br/>Trend5/Accum1"]
        G3 -->|RISK_ON| T2["禁止正式新开 overheat_shadow<br/>研究配额 5/1"]
        G3 -->|RISK_OFF| T3["提高门槛 + 禁新开"]
        G3 -->|CRASH| T4["极限门槛 + 禁新开"]
    end

    subgraph LAYERS["主漏斗与候选车道"]
        L1["L1 layer1_filter<br/>A股支持板块 · 非 ST · 市值≥25亿<br/>成交额≥4000万 · 财务过滤"]
        ML["Mainline Engine<br/>动态主线发现<br/>概念热度 + 主题雷达 + 财务质量"]
        L2["L2 layer2_strength_detailed<br/>八通道并行"]
        L2A["主升 Markup"]
        L2B["潜伏 Ambush"]
        L2C["吸筹 Accumulation"]
        L2D["地量 Dry Volume"]
        L2E["暗中护盘 RS Divergence"]
        L2F["趋势延续 Trend Continuation"]
        L2G["加速突破 Breakout Acceleration"]
        L2H["点火破局 SOS Bypass"]
        L3["L3 layer3_sector_resonance<br/>行业/概念共振<br/>强个股与主线绕行"]
        L4["L4 layer4_triggers<br/>SOS / Spring / LPS / EVR / Compression / Trend Pullback"]
        LN["Candidate Lane<br/>趋势回踩 / 平台突破 / 强承接"]
        MLBUY["主线买点候选<br/>timing_score 过关"]
        L5["L5 layer5_exit_signals<br/>派发 / 止损预警"]
    end

    subgraph BYPASS["Shadow 观察池（默认不进正式 AI）"]
        B1["L2 明珠旁路<br/>L1过 + L2拒 + 热门板块 + L4"]
        B2["战略 L2 旁路<br/>主题雷达观察池 + 阶段复核"]
        B3["外部观察 Shadow<br/>人工关注只验证，不直接推荐"]
    end

    subgraph POST["后处理 & 候选分配"]
        R1["watch_score 排序 L3"]
        R2["Markup / Accum 阶段识别"]
        R3["主题雷达 theme_radar 构建"]
        R4["候选评分 + 三轨分配"]
        R4A["Trend 轨：主升 + 点火 + 趋势延续 + 加速突破"]
        R4B["Accum 轨：潜伏 + 吸筹 + 地量 + 护盘"]
        R4C["Mainline 轨：主线买点候选"]
        R5{"FUNNEL_AI_SELECTION_MODE"}
        R5 -->|all_formal_l4| R6["正式 L4 全量送 AI<br/>不含 L3 补位"]
        R5 -->|quota 当前默认| R7["按 regime 静态配额<br/>FUNNEL_AI_*_TREND/ACCUM"]
        R8{"FUNNEL_DYNAMIC_POLICY"}
        R8 -->|off| R9["静态配额"]
        R8 -->|shadow| R10["静态出结果 + shadow 差异写库"]
        R8 -->|on| R11["读 signal_health/registry 动态配额"]
        R12["候选车道 / 主线候选<br/>按配额加权送审"]
        R15["统一损失护栏<br/>纯SOS ABC=3/3<br/>单EVR/LPS/TrendPB默认观察"]
        R14["Shadow 观察<br/>只验证不入 AI"]
        R16{"数据质量门禁<br/>OHLCV/市值≥95%<br/>财务≥90%（请求时）"}
        R17["degraded / observe_only<br/>保留 AI/shadow 观察<br/>禁止正式推荐与新开仓"]
        R13["飞书推送漏斗报告"]
    end

    PREP --> GATE --> L1
    P1 --> P8
    L1 --> L2
    L1 --> ML
    L1 --> LN
    L2 --> L2A & L2B & L2C & L2D & L2E & L2F & L2G & L2H
    L2A & L2B & L2C & L2D & L2E & L2F & L2G & L2H --> L3 --> L4
    ML --> MLBUY
    MLBUY --> POST
    LN --> POST
    L1 --> BYPASS
    L4 --> L5
    L4 --> R15 --> POST
    BYPASS --> POST
    P8 --> POST
    L5 --> POST
    POST --> R16
    R16 -->|覆盖达标| R13
    R16 -->|覆盖不足| R17 --> R13
```

### 正式候选来源

| 来源 | 进入条件 | 是否可直接买 |
|------|----------|--------------|
| 传统 Wyckoff | L1/L2/L3 后出现 L4 信号 | 不直接买，先进入 AI/二次确认 |
| 主线候选 | `mainline_score` 达标，且 timing gate 过关 | 不直接买，仍需 AI/尾盘确认 |
| 候选车道 | 趋势回踩、平台突破、强承接等结构接近 | 默认观察，质量足够才按配额送审 |
| Shadow 旁路 | L2 未过但有复盘价值，或外部观察名单 | 不进入正式 AI，除非显式打开开关 |

正式候选在送入 Step3 前还会经过 `core/candidate_policy.py` 的统一损失护栏。纯 SOS 必须通过 Springboard ABC 3/3；单 EVR、单 LPS 与单 Trend Pullback 默认仅观察；弱确认、过热和高位追涨样本会被剪除。L2 的八通道原始命中数会写入诊断日志，但不参与评分。

### 数据质量与诊断口径

- OHLCV 和市值覆盖率均不得低于 95%；请求财务指标时，财务覆盖率不得低于 90%。
- 任一必需覆盖率不足，运行状态标记为 `degraded`，交易就绪度强制为 `observe_only`。候选仍可进入 AI/shadow 对照，但报告、结构化详情和候选行都会禁止正式推荐、写入执行清单或新开仓。
- 报告展示三个覆盖率、OHLCV 数据源数量与占比、RPS universe 数量，以及 L1 到 L4 的输入、通过、淘汰数量和该层筛选原因。
- L2 保留多标签；没有通道命中时返回空标签，不再兜底伪装成“点火破局”。概念聚合按股票稳定去重，同一股票不会对同一概念重复计数。

### L4 触发信号

| 信号 | 含义 | 典型轨道 |
|------|------|----------|
| SOS | 放量突破 | Trend |
| Spring | 假跌破收回 | Accum |
| LPS | 缩量回踩 | Accum |
| EVR | 放量不跌 | Trend |
| Compression | 压缩蓄势 | 通用 |
| Trend Pullback | 趋势回踩 | Trend / Mainline |

### 外部观察名单

`external_seeds` 用于把人工关注、社区反馈或其它系统给出的股票加入同一套漏斗观察，而不是作为正式候选来源：

- 配置来源：`config/profiles/a_share_prod.yml`、`FUNNEL_EXTERNAL_SEED_SYMBOLS`、`FUNNEL_EXTRA_SYMBOLS` 或 `symbols_file`
- 默认只做 shadow 观察：记录是否通过 L1/L2、是否在 L2 后触发 L4、是否过期
- 外部观察名单固定为 shadow-only，不进入 `selected_for_ai`
- 通过 L4 的外部观察对象会额外写入 `signal_observations`，`selection_mode=external_seed_shadow`

---

## 四、Step3 AI 研报流程

```mermaid
flowchart LR
    IN["symbols_info<br/>漏斗候选 + 元数据"] --> FETCH["逐只拉 OHLCV<br/>320 日窗口"]
    FETCH --> FEAT["特征工程<br/>generate_stock_payload<br/>均线/量价切片/高光事件"]
    FEAT --> SPLIT["双轨分组<br/>Trend vs Accum"]
    SPLIT --> LLM["LLM 三阵营审判<br/>逻辑破产 / 储备营地 / 起跳板"]
    LLM --> RAG["RAG 语义防雷<br/>rag_veto 新闻否决"]
    RAG --> OUT["extract_operation_pool_codes<br/>提取起跳板代码"]
    OUT --> PUSH["飞书/企微/钉钉推送研报"]
    OUT --> MARK["mark_ai_recommendations<br/>recommendation_tracking"]
    OUT --> CONF["跨日信号确认<br/>SOS/EVR需守支撑+站稳MA20"]
```

**LLM 配置**（workflow 默认）：

- Step3：`STEP3_LLM_PROVIDER=gemini`，fallback `efficiency`
- 输入不是原始 K 线，而是压缩后的结构特征
- 空候选仍发送空集报告、合规摘要和明日执行结论；主 provider 失败时按配置回退，不会在 daily-job 包装层静默跳过

---

## 五、Step4 OMS 持仓决断

```mermaid
flowchart TD
    IN1["Step3 研报文本"] --> S4
    IN2["起跳板 candidate_meta"] --> S4
    IN3["Supabase portfolios<br/>USER_LIVE:user_id"] --> S4
    IN4["market_signal_daily<br/>benchmark + premarket"] --> S4
    IN5["TickFlow 持仓分时诊断"] --> S4

    S4["run_step4()"] --> IDEM{"幂等检查<br/>同日同持仓快照已跑?"}
    IDEM -->|是| SKIP["跳过"]
    IDEM -->|否| LLM["LLM 决策<br/>EXIT > TRIM > HOLD > PROBE/ATTACK"]

    LLM --> RISK{"风控门控"}
    RISK -->|RISK_ON / BEAR_REBOUND / PANIC_REPAIR / RISK_OFF / CRASH / BLACK_SWAN| BLOCK_BUY["冻结新开仓<br/>STEP4_BUY_BLOCK_REGIMES"]
    RISK -->|NEUTRAL / CAUTION| ALLOW["按交易模式限额执行"]

    ALLOW --> OMS["灾难止损地板 -12%<br/>PROBE≤10% / ATTACK≤20%<br/>ATR/结构/时间管理优先"]
    OMS --> TG["推送工单（含执行纪律）"]
    OMS --> DB["trade_orders 写库"]
```

### 报告执行纪律

日漏斗、Step3、尾盘、OMS 推送正文顶部固定附带 `core/execution_playbook.py` 的 **「🧭 执行纪律」**（闸门、主线优先、5 日持有、-12% 灾难地板）。操作解读见 [`OPERATOR_PLAYBOOK.md`](OPERATOR_PLAYBOOK.md)。

---

## 六、尾盘执行层（与日漏斗串联）

| 项 | 说明 |
|----|------|
| 入口 | `tail_buy_1440.yml` → `scripts/tail_buy_intraday_job.py` |
| 候选 | 读 `signal_pending`；**confirmed 才可 BUY** |
| 排序 | confirmed → 主线/趋势 → 信号分 |
| 主线语义 | `candidate_theme / candidate_phase / candidate_role` 从推荐、信号贯穿到尾盘记录；LLM 只解释不重判 |
| 禁新开 | `RISK_ON` 与弱市/修复期与 Step4 对齐，新票不买 |
| 持仓 | 硬止损约 12%；非主线满 5 日建议时间止盈 |
| 读法 | 只执行 **BUY（可执行）**；WATCH/SKIP 不下手 |

**日漏斗 = 候选池；尾盘 = 今天买不买。缺一不可。**

---

## 七、跨日反馈闭环

漏斗与 feedback 是**错峰运行**的反馈系统：漏斗先产出观察样本，feedback 盘后验收，下一轮漏斗再读取新的策略状态。详见 [`SIGNAL_FEEDBACK_LOOP.md`](SIGNAL_FEEDBACK_LOOP.md)。

```mermaid
sequenceDiagram
    participant T1 as Day N 17:17 漏斗
    participant OBS as signal_observations
    participant REC as recommendation_tracking
    participant FB as Day N 23:30 feedback
    participant HL as signal_health_daily
    participant REG as signal_registry
    participant T2 as Day N+1 17:17 漏斗

    T1->>OBS: L4 命中 + AI 起跳板标记
    T1->>REC: 形态复盘记录
    Note over T1: signal_pending 写入待确认

    FB->>OBS: 读取观察样本
    FB->>FB: 拉后续 K 线计算 1/3/5/10/20 日 outcomes
    FB->>HL: 聚合胜率/均值收益/权重
    FB->>REG: 更新信号启停状态

    T2->>HL: FUNNEL_DYNAMIC_POLICY=on 时读权重
    T2->>REG: 过滤失效信号类型
    T2->>OBS: 新一轮观察样本
```

### `FUNNEL_DYNAMIC_POLICY` 模式

| 模式 | 行为 |
|------|------|
| `off` | 默认静态 Trend / Accum 配额，不读取反馈权重 |
| `shadow` | 主流程保持静态配额，动态策略读取 health / registry / 归因调权后把差异写入 `signal_policy_shadow_runs` |
| `on` | 正式使用 `signal_health_daily` 权重、`signal_registry` 启停状态和归因调权 |

归因报告的晋级判断不只看一句 `review_promote_dynamic_policy`。Agent、Web 和 CLI 都应优先读取
`latest_operator_summary` / `operator_summary`、`latest_policy_display` 与 `latest_execution_summary`：
`operator_summary` 先给一行运营结论，display 字段给出下一步动作、正式 dynamic 状态和当前生效范围；
raw `next_action` / `promotion_status` 只用于追证据；`promotion_checklist` 固定检查 shadow 样本、
shadow 新增表现、scoped 信号调权和回测确认。
只有这些证据持续通过，才考虑把 `FUNNEL_DYNAMIC_POLICY` 从 `shadow` 切到 `on`。

---

## 八、并行下游任务时间线

| 时间（北京） | 工作流 | 与漏斗关系 |
|-------------|--------|-----------|
| **08:20** | `premarket_risk.yml` | **上游门控**：A50 + VIX → Step4 次日买入权限 |
| **周日-周四 17:17** | `wyckoff_funnel.yml` | **主漏斗** daily_job Step2→3→4；周日正常为周一实盘准备候选，次日非 A 股交易日才跳过 |
| **19:25** | `review_list_replay.yml` | 下游：涨停复盘 |
| **21:10 周五** | `theme_radar.yml` | 下游：主线雷达周报（新闻增强） |
| **23:00 周一-周五** | `recommendation_tracking_reprice.yml` | 下游：复盘重定价 |
| **次日 06:20 周二-周六** | `db_maintenance.yml` | 下游：清理过期数据 |
| **23:30 周一-周五** | `signal_feedback.yml` | **下游反馈**：刷新 health / registry |
| **次日尾盘** | `tail_buy_1440.yml` | **下游执行**：手动或外部自动化触发，读 `signal_pending` 尾盘策略；pending 只观察，confirmed 才可 BUY |

---

## 九、Supabase 数据流

```mermaid
flowchart LR
    subgraph STEP2_WRITE["Step2 写入"]
        W1["market_signal_daily<br/>regime / 指数"]
        W2["theme_radar_snapshot"]
        W3["signal_pending<br/>待确认信号"]
        W4["recommendation_tracking<br/>形态复盘"]
        W12["external_seed_observations<br/>外部观察验证"]
    end

    subgraph STEP3_WRITE["Step3 写入"]
        W5["recommendation_tracking<br/>AI 起跳板标记"]
        W6["signal_observations<br/>L4 观察样本"]
    end

    subgraph STEP4_WRITE["Step4 写入"]
        W7["trade_orders<br/>买卖建议"]
    end

    subgraph FEEDBACK["23:30 反馈"]
        W8["signal_outcomes"]
        W9["signal_health_daily"]
        W10["signal_registry"]
        W11["signal_policy_shadow_runs"]
    end

    STEP2_WRITE --> FEEDBACK
    STEP3_WRITE --> FEEDBACK
    FEEDBACK -->|下一轮漏斗读取| STEP2_WRITE
```

---

## 十、数据源降级链（OHLCV）

```
TickFlow (优先, qfq 前复权)
  ↓ 失败
Tushare
  ↓ 失败
AkShare
  ↓ 失败
Baostock
  ↓ 失败
efinance
```

- 批量参数：`BATCH_SIZE=200`，`MAX_WORKERS=4`，320 交易日窗口
- 快照：`data/funnel_snapshots/`（供回测离线使用）

---

## 十一、当前生产配置要点

来源：`.github/workflows/wyckoff_funnel.yml`

| 变量 | 当前值 | 作用 |
|------|--------|------|
| `FUNNEL_AI_SELECTION_MODE` | `tradeable_l4` | 只把可交易 L4 结构送入 Step3，减少裸 SOS/EVR 追高噪声 |
| `FUNNEL_AI_TOTAL_CAP` | `8` | AI 总量硬上限；战略/主题补位也受此限制 |
| `FUNNEL_DYNAMIC_POLICY` | `shadow` | 主流程用静态配额，同时记录动态策略差异 |
| `FUNNEL_AI_NEUTRAL_TREND` / `FUNNEL_AI_NEUTRAL_ACCUM` | `5` / `1` | 中性市主线/趋势主导，Accum 仅残量 |
| `FUNNEL_AI_RISK_ON_TREND` / `FUNNEL_AI_RISK_ON_ACCUM` | `5` / `1` | 过热市 AI/shadow 研究配额；正式推荐与新开仓由市场闸门禁止 |
| `FUNNEL_EXTERNAL_SEED_SYMBOLS` / `FUNNEL_EXTRA_SYMBOLS` | 空 | 临时追加外部观察名单；存在时自动启用 external seed shadow |
| `STEP4_BUY_HARD_STOP_PCT` | `12.0` | 新开仓灾难止损地板；ATR/结构/时间管理优先 |
| `STEP4_REQUIRE_CONFIRMED_BUY_CANDIDATE` | `1` | Step4 新开仓只允许二次确认候选；未确认候选只观察 |
| `TAIL_BUY_CONFIRMED_ONLY_BUY` | `1` | 尾盘买入只对二次确认候选输出 BUY |
| `TAIL_BUY_HOLDING_HARD_STOP_PCT` | `12` | 持仓尾盘诊断的固定止损兜底；ATR 放宽需显式开启且受上限约束 |
| `STEP4_BUY_BLOCK_REGIMES` | `RISK_ON,BEAR_REBOUND,PANIC_REPAIR,RISK_OFF,CRASH,BLACK_SWAN` | 过热与弱市冻结新开仓 |

---

## 相关文档

| 文档 | 内容 |
|------|------|
| [`OPERATOR_PLAYBOOK.md`](OPERATOR_PLAYBOOK.md) | **实盘怎么用**：日漏斗 × 尾盘串联纪律 |
| [`README_STRATEGY.md`](../README_STRATEGY.md) | 策略逻辑、L1–L5、配额、尾盘与 OMS |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 架构、Actions 全表、Supabase 表结构 |
| [`SIGNAL_FEEDBACK_LOOP.md`](SIGNAL_FEEDBACK_LOOP.md) | 信号反馈闭环详解 |
| [`GLOSSARY.md`](../GLOSSARY.md) | 术语速查 |
