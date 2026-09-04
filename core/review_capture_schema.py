"""review_capture_daily 的建表语句。

这张表回答一个问题:**漏斗有没有在变得更会挑票**——具体到可检验的形式,是
「今日 >7% 的强势股里,前一日漏斗捕获了多少,比同日基准率高吗;每道闸门放开
一层又能多捞到几只,增量精度比基准高吗」。

为什么必须落库,而不是继续看每日报告:
- 单日样本没有判别力。2026-09-03 那天捕获率 3/63=4.8% vs 基准 4.0%,p=0.740;
  题材闸放开多捞 6 只、增量精度 1.12% vs 基准 1.37%,p=0.851。两个都是「看不出
  差别」,不是「没差别」——n=1 天本来就分不出。要 20+ 个交易日才谈得上结论。
- **补不回来**。复盘行依赖前一日 trace(哪只卡在哪层),trace 只活在
  `daily-job-artifacts-*` 里(retention-days: 30);复盘自己的产物在
  `review-list-replay-logs-*` 里只留 7 天。两个都过期后,那一天永久算不出来。
  所以每过一天没留存就永久少一天样本——这是唯一有时钟压力的部分。
- 体积不是障碍:强势复盘池每天约 50~70 只,一年不到两万行。

**这张表不能回答什么**(写清楚,否则以后必被误读):
单日 >7% 的脉冲不是漏斗的目标形态(漏斗奔的是 T+5/T+10 的吸筹到拉升),所以
「捕获率高」不等于「赚钱」,追这种脉冲甚至可能是负贡献。绝对收益口径由
`review_shadow_lane_daily` 的 T+5/T+10 同动量对照回答,不要拿这张表代替它。

口径上的两条硬约束:
1. **基准率必须逐日算再合并,不能全局算一次。** 复盘池本身是按「今日 >7% 且
   前一日 <3%」挑出来的,即按结果选样;只有拿同一天的同层分母作对照,这个
   召回率才有意义。所以每行都带当日的 universe/L1/L2/L3/候选 五个分母。
2. **`gain_pct` 是选样条件,不是前瞻收益。** 它就是「今日涨幅 >7%」里的那个
   涨幅,拿它当收益去评估会直接构成循环论证(见 memory
   control-row-must-measure-itself:对照行必须量自己)。

本项目不保留 .sql 文件(scripts/quality_gate.py 会拦),且 Supabase Python SDK 走
REST 不支持 DDL、环境也无 psycopg2/asyncpg 与连接串,故 schema 以 Python 常量形式
版本化,由 scripts/print_review_capture_ddl.py 打印后人工在 SQL Editor 执行一次。
"""

from __future__ import annotations

from core.constants import TABLE_REVIEW_CAPTURE_DAILY

# 与 integrations.supabase_review_capture.build_capture_rows 的 payload 键一一对应。
# 顺序即建表顺序;测试会校验两者不漂移。
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("trade_date", "date not null", "复盘日(今日异动发生的交易日)"),
    ("previous_trade_date", "date not null", "被复盘的漏斗运行日(前一交易日)"),
    ("ts_code", "text not null", "股票代码,6 位数字"),
    ("name", "text", "股票名称,便于人工核对"),
    # 归因:这只票在前一日漏斗里卡在哪一层。
    ("stage", "text not null", "级联归因档位。注意档位顺序 != 淘汰原因,见下面的分母说明"),
    ("reason", "text", "该档位的具体理由,含闸门数值"),
    ("l1_eligible", "boolean not null default false", "前一日是否过基础准入"),
    ("l2_eligible", "boolean not null default false", "前一日是否过结构强度"),
    ("l3_eligible", "boolean not null default false", "前一日是否过题材共振"),
    ("is_candidate", "boolean not null default false", "前一日是否进入候选池"),
    ("trigger_labels", "text[]", "买点触发标签。只对候选池内的票有值——触发检测只跑最终候选集"),
    ("risk_signal", "text", "风控信号名,如 stop_loss"),
    ("tracked_previous_day", "boolean not null default false", "前一日是否已在推荐跟踪表里"),
    ("ai_recommended_previous_day", "boolean not null default false", "前一日是否被 AI 推荐"),
    # 影子车道:与 review_shadow_lane_daily 同源判定,便于两张表对照。
    ("shadow_lane", "text", "影子车道名,无则 null"),
    ("shadow_score", "numeric(10, 4)", "影子车道排序键,无连续键时为 null——不要填常数"),
    # 可执行性:能不能真买到。写入闸与下单闸曾各读一套配置(见 memory
    # two-gates-must-share-one-source),所以这两个标记必须落下来。
    ("open_executable", "boolean not null default false", "次日开盘是否可执行(未一字板/未涨停)"),
    ("intraday_executable", "boolean not null default false", "次日盘中是否可执行"),
    ("open_gap_pct", "numeric(10, 4)", "次日开盘跳空幅度,百分数"),
    # 选样条件本身。命名带 gain 而非 return,避免被当成前瞻收益。
    ("gain_pct", "numeric(10, 4)", "复盘日涨幅(选样条件 >7%),**不是**前瞻收益,不可用于评估"),
    # 当日分母:基准率必须逐日算再合并,否则按结果选样的召回率没有意义。
    ("pool_size", "integer not null", "当日复盘池规模(分母)"),
    ("universe_count", "integer not null", "前一日全市场标的数"),
    ("l1_count", "integer not null", "前一日过 L1 的数量"),
    ("l2_count", "integer not null", "前一日过 L2 的数量"),
    ("l3_count", "integer not null", "前一日过 L3 的数量"),
    ("candidate_count", "integer not null", "前一日候选池规模"),
    ("context_source", "text", "前一日漏斗上下文来源:trace 快照还是完整重跑"),
    ("created_at", "timestamptz not null default now()", "写入时间"),
)

# 一个复盘日一只票只应有一行。重跑同一天覆盖而非累积。
# 键必须与写入侧 on_conflict 完全一致——见 memory dedup-key-must-match-db-constraint:
# 键错位会让一行冲突回滚整批,而且是静默的。
UNIQUE_KEY = ("trade_date", "ts_code")


def payload_keys() -> frozenset[str]:
    """build_capture_rows 应写入的字段（不含自增 id 与默认 created_at）。"""
    return frozenset(name for name, _, _ in COLUMNS if name != "created_at")


def build_ddl() -> str:
    """生成完整建表语句。幂等——可重复执行。"""
    table = TABLE_REVIEW_CAPTURE_DAILY
    body = ["    id bigserial primary key,"]
    for name, ddl_type, comment in COLUMNS:
        body.append(f"    -- {comment}")
        body.append(f"    {name} {ddl_type},")
    body.append(f"    constraint {table}_unique unique ({', '.join(UNIQUE_KEY)})")
    lines = [
        f"create table if not exists public.{table} (",
        *body,
        ");",
        "",
        "-- 主查询：按档位随时间聚合,算逐日捕获率与各闸门增量精度。",
        f"create index if not exists {table}_stage_idx",
        f"    on public.{table} (trade_date desc, stage);",
        "",
        "-- 单票追溯：某只票反复被漏掉时,看它每次卡在哪。",
        f"create index if not exists {table}_code_idx",
        f"    on public.{table} (ts_code, trade_date desc);",
    ]
    return "\n".join(lines) + "\n"
