# A 股操作手册（日漏斗 × 次日开盘）

> 本文是**实盘怎么用**的单一口径。策略细节见 [`README_STRATEGY.md`](../README_STRATEGY.md)，执行链路见 [`A_SHARE_FUNNEL_FLOW.md`](A_SHARE_FUNNEL_FLOW.md)。

---

## 1. 一句话原则

**日漏斗定候选与环境；跨日需求确认定今天能不能买。**
两者串联，不是二选一。

```text
日漏斗（盘后）→ 主线候选名单
  → Step3 起跳板（建议）
  → SURVIVED / VALIDATED 跨日验证
  → OMS 生成唯一允许买入区间
  → 次日开盘价位于区间内才下单
```

---

## 2. 双书结构

| 书 | 做什么 | 配额/仓位 | 持有 |
|----|--------|-----------|------|
| **主线趋势书** | 主题连续 + 高 RPS + 回踩 MA5/MA10/MA20 或平台再突破 | 主仓 70–80% | 约 15 日；破 MA20 / 主题缩量阴跌再减 |
| **结构观察书** | Spring / LPS / Compression 等经典结构 | 轻仓或观察 | **默认 5 日**时间止盈 |

NEUTRAL 默认采用质量池：形态达标者共同排序，最终最多 **8 只**、单行业最多 **2 只**。
Trend/Accum 配额只用于 dynamic shadow 对照；RISK_ON 市场闸门仍禁止正式推荐和新开仓。

---

## 3. 市场闸门（先看报告顶部）

| 水温 / 模式 | 新开仓 | 你怎么做 |
|-------------|--------|----------|
| **NEUTRAL**（`mainline_active`） | 允许 | 只做主线确认链路 |
| **CAUTION** | 最多一只小额试探仓 | 必须二次确认，只允许 PROBE，禁止 ATTACK |
| **RISK_ON** | **禁止** | 只管理旧仓 |
| **BEAR_REBOUND / PANIC_REPAIR** | 禁止自动开仓 | 修复候选仅观察，等待次日广度/价格确认 |
| **PANIC_REPAIR_CONFIRMED** | 最多一只小额试探仓 | 仅允许 PROBE，禁止 ATTACK、追价和自动扩仓 |
| **RISK_OFF / CRASH / BLACK_SWAN** | **默认禁止** | 现金/减仓优先；只影子观察 |

报告顶部固定有 **「🧭 今日执行纪律」**，先读纪律再读候选。

---

## 4. 每日流程

### 盘后：日漏斗报告

1. 读 **执行纪律** + **今日交易模式**（禁止新仓则明日不新开）
2. 只记 **主线买点候选**（0–3 只）；旁路/Accum 不当主仓
3. 等 Step3 **起跳板**（储备营地 = 不动）

### 次日：跨日确认 + 开盘买入

1. 只对昨日名单里的票看信号是否已 `VALIDATED`（数据库兼容字段为 `confirmed`）
2. **只有 VALIDATED 才能继续送 OMS**；`pending`/`survived`/`未确认`/`观察` = 不买
3. 仅当开盘价位于 OMS 的“明日允许买入区间”内才执行；高于上界不追，低于下界不抄底，无支撑、破支撑或禁新开水温同样不买

### 持仓

- 跟 **持仓诊断**（`workflows/holding_diagnosis_core.py`）+ **Step4 OMS**
- 优先级：`EXIT/TRIM > HOLD > PROBE/ATTACK`
- 非主线满 **5 日**优先时间止盈；灾难地板约 **-12%**（不是日常洗盘线）

### 成交后必须回填

持仓表由人工维护，OMS 只发建议、不会自动改股数。**成交没录入，系统就还以为你拿着那只票**，
于是每天重新发同一条 EXIT，止损形同虚设，净值也跟着失真。

```bash
wyckoff portfolio fill 603661 --side sell --shares 600 --price 27.69
wyckoff portfolio fill 600519 --side buy  --shares 100 --price 1680 --date 20260728
```

也可以直接对 Agent 说「我卖了 603661 六百股，成交 27.69」，走 `record_trade_fill` 工具。
回填会按成交增量摊薄成本价、扣掉佣金印花税、卖光时清仓，并给出已实现盈亏；
`portfolio add` 是覆盖式录快照，不要拿它记成交。

工单顶部若出现「未执行的离场工单」，说明某只票的 EXIT 已连发多日仍未落地——
要么去券商补掉这一笔，要么回填你实际已经成交的记录。

工单底部的现金是“若全部工单成交后的预计可用现金”，不是券商实时余额。未成交 `EXIT`
不会再把模型给出的历史破位价写回持仓；若新仓工单出现同时高于成本和现价的止损，OMS 会将
该离场动作降级为 `HOLD`。此时应先核对买入日期和原始入场失效位，不要把倒挂价当保护止损。

如果这只票的**现价还在止损线下方**，系统会拒绝 ATTACK 重仓（变成 `NO_TRADE`，理由里列出欠着的代码）；
小额 PROBE 试探仓、离场、减仓都不受影响。想临时解除，设 `STEP4_BLOCK_BUY_ON_STALE_EXIT=0`。

闸门刻意收得比告警窄，因为两种「拖着没卖」性质不同：没落袋的止盈拖着只是少赚，
跌破止损还拿着才是风控失效。一字跌停当天卖不掉，那一天不计入拖延天数。

---

## 5. 下单检查清单（缺一不可）

- [ ] 水温允许新开（不是 RISK_ON / 弱市）
- [ ] 来自主线书（或明确轻仓的结构票）
- [ ] Step3 为起跳板（若有研报）
- [ ] 信号 **VALIDATED**（库内 `confirmed`；`pending`/`survived` 只观察）
- [ ] 次日开盘价位于 OMS 的唯一允许买入区间内

---

## 6. 报告上哪里看规则

| 报告 | 纪律位置 |
|------|----------|
| 日漏斗飞书卡 | 顶部「今日执行纪律」+ 候选清单说明 |
| Step3 研报输入 | 宏观水温前的「执行纪律」 |
| 持仓诊断报告 | 统计后、ADD/TRIM/HOLD 列表前 |
| Step4 OMS 工单 | 市场视图后 |
| 持仓上下文 | 「时间管理：TIME_EXIT / HOLD …」 |

---

## 7. 常见错误

1. 日漏斗出票就开盘追 → 买早
2. 把 `survived` 当成 `confirmed`/`VALIDATED` → 为了跨日而跨日
3. RISK_ON 仍新开 → 与闸门对着干
4. 用 -7% 当日常止损砍主升 → 被洗盘打掉
5. 把观察池 / 旁路当主仓 → 负期望堆仓
6. 成交后不回填 → 止损只出现在推送里、从不落地；生产上出现过同一只票连发 12 天 EXIT、
   期间又跌 19% 的情况，而净值表当时显示的是「已清仓」

### 策略语义变更后的历史回刷

LPS、确认状态或候选血缘发生语义变更时，先做 dry-run，检查 `old_rows.json`、新 payload 和每日计数；未经人工验收不得直接写生产库：

```bash
.venv/bin/python scripts/backfill_recommendation_tracking.py \
  --dates 2026-07-01,2026-07-02 \
  --output-dir artifacts/recommendation_backfill/lps-v2 \
  --skip-step3
```

实际范围应包含规则上线后受影响的全部交易日。`--apply` 会替换对应日期的 `recommendation_tracking`、`signal_pending` 和 `signal_observations`；删除 observation 会级联删除对应 outcome，因此应用后必须重跑 `scripts/signal_feedback_job.py`，再核对 `signal_health_daily` 与 `signal_registry`，不能只回刷推荐表。

---

## 8. 相关代码与配置

| 模块 | 路径 |
|------|------|
| 交易模式 | `core/market_trade_mode.py` |
| AI 配额 | `core/ai_candidate_allocation.py` |
| 执行纪律文案 | `core/execution_playbook.py` |
| 持有时间 | `core/holding_time_policy.py` |
| 持仓诊断 | `workflows/holding_diagnosis_core.py` + `core/holding_diagnostic.py` |
| 生产 env | `.github/workflows/wyckoff_funnel.yml`、`holding_diagnosis.yml` |
