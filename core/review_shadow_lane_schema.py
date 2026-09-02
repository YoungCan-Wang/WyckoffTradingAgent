"""review_shadow_lane_daily 的建表语句。

为什么要落库,而不是继续靠 artifact:
- trace 只活在 `daily-job-artifacts-*` 里,retention-days: 30(与日志同包),
  约 20 个交易日。同动量对照的效果检验做不到 20 个观测就收敛。
- 更要紧的是**补不回来**:回测引擎(workflows/backtest_*)不重放漏斗分层,
  没有任何路径能从快照倒推出「某日某票卡在哪一层、watch_score 多少」。
  所以每过一天没留存,就永久少一天样本——这是唯一有时钟压力的部分。
- 体积不是障碍:全市场约 5000 只的 trace 压缩后约 33KB/日,一年 7.8MB。
  但这里只落**有影子车道的行**(约 100~200 行/日),把表压到能直接查。

本项目不保留 .sql 文件(scripts/quality_gate.py 会拦),且 Supabase Python SDK 走
REST 不支持 DDL、环境也无 psycopg2/asyncpg 与连接串,故 schema 以 Python 常量形式
版本化,由 scripts/print_review_shadow_lane_ddl.py 打印后人工在 SQL Editor 执行一次。

字段设计的两条约束:
1. **动量必须同期落下来。** 效果检验要的是「同动量随机对照」(见 memory
   full-market-control-confounds-momentum / uniform-band-control-is-biased):
   不记录当日 RPS,事后就只能拿全市场当对照,把择时读成选股。rps_fast/rps_slow
   是 L1 闸门自己算过的值,不额外计算。
2. **score 可空 + ranked 显式。** rotation_setup 所在的题材共振层是集合成员
   判断,没有连续键;老 trace 也可能缺 watch_score。宁可存 null 并标
   ranked=false,也不要用常数占位——那正是 v1 让 31 只票同分、排不了序的原因。
"""

from __future__ import annotations

from core.constants import TABLE_REVIEW_SHADOW_LANE_DAILY

# 与 integrations.supabase_review_shadow_lane.save_review_shadow_lane_rows 的
# payload 键一一对应。顺序即建表顺序;测试会校验两者不漂移。
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("trade_date", "date not null", "信号日(决策发生的交易日),非写入日"),
    ("ts_code", "text not null", "股票代码,6 位数字"),
    ("name", "text", "股票名称,便于人工核对"),
    ("sector", "text", "所属行业"),
    ("lane", "text not null", "影子车道:near_l2 / rotation_setup / pre_breakout"),
    ("stage", "text not null", "当日卡在漏斗哪一层(复盘阶段标签)"),
    ("score", "numeric(10, 4)", "车道排序键,0~100。无连续键时为 null——不要填常数"),
    ("ranked", "boolean not null default false", "score 是否可用于排序。false 时该行只作标签"),
    ("reason", "text", "车道判定理由,含 watch_score 或缺口等具体数值"),
    ("watch_score", "numeric(10, 6)", "L3 原始 watch_score(约 [0,1]),pre_breakout 的排序键来源"),
    ("l2_channel", "text", "已通过的结构通道名"),
    ("l1_eligible", "boolean not null default false", "当日是否过基础准入"),
    ("l2_eligible", "boolean not null default false", "当日是否过结构强度"),
    ("l3_eligible", "boolean not null default false", "当日是否过题材共振"),
    # 动量:同动量对照的必要条件。缺了它只能拿全市场比,那会把择时当选股。
    ("rps_fast", "numeric(10, 2)", "当日快线 RPS(默认 20 日),L1 闸门同源"),
    ("rps_slow", "numeric(10, 2)", "当日慢线 RPS(默认 120 日),L1 闸门同源"),
    ("close", "numeric(14, 4)", "信号日收盘价,用于核对入场价口径"),
    ("policy_version", "text not null", "影子车道策略版本,如 review_shadow_v2"),
    ("created_at", "timestamptz not null default now()", "写入时间"),
)

# 一个信号日一只票在一条车道上只应有一行。重跑同一天覆盖而非累积。
# 键必须与写入侧 on_conflict 完全一致——见 memory dedup-key-must-match-db-constraint:
# 键错位会让一行冲突回滚整批,而且是静默的。
UNIQUE_KEY = ("trade_date", "ts_code", "lane")


def payload_keys() -> frozenset[str]:
    """save_review_shadow_lane_rows 应写入的字段（不含自增 id 与默认 created_at）。"""
    return frozenset(name for name, _, _ in COLUMNS if name != "created_at")


def build_ddl() -> str:
    """生成完整建表语句。幂等——可重复执行。"""
    table = TABLE_REVIEW_SHADOW_LANE_DAILY
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
        "-- 主查询：某条车道随时间的表现,按信号日倒序取最近样本。",
        f"create index if not exists {table}_lane_idx",
        f"    on public.{table} (lane, trade_date desc);",
        "",
        "-- 同动量对照要按 (信号日, 动量) 找邻居,单独走一条索引。",
        f"create index if not exists {table}_momentum_idx",
        f"    on public.{table} (trade_date, rps_slow);",
    ]
    return "\n".join(lines) + "\n"
