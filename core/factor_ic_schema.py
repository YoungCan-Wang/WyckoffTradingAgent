"""factor_ic_daily 的建表语句。

本项目不保留 .sql 文件（scripts/quality_gate.py 会拦），且 Supabase Python SDK 走
REST 不支持 DDL、环境也无 psycopg2/asyncpg 与连接串，故 schema 以 Python 常量形式
版本化，由 scripts/print_factor_ic_ddl.py 打印后人工在 SQL Editor 执行一次。

这样做的好处是 schema 能被测试覆盖——tests 会断言字段与 save_factor_ic_rows 写入的
键一致，避免代码改了字段而建表语句没跟上。
"""

from __future__ import annotations

from core.constants import TABLE_FACTOR_IC_DAILY

# 与 integrations.supabase_factor_ic.save_factor_ic_rows 的 payload 键一一对应。
# 顺序即建表顺序；测试会校验两者不漂移。
COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("eval_date", "date not null", "评估运行日（非行情日），同一 run 的所有因子共享"),
    ("factor_name", "text not null", "因子名，与 scan_factor_ic.build_factors 的键一致"),
    ("horizon", "int not null", "前瞻交易日数（5 / 10）"),
    ("segment", "text not null default 'full'", "full=全样本；seg1/seg2/... =滚动分段"),
    ("window_start", "date", "行情区间起，便于复现"),
    ("window_end", "date", "行情区间止"),
    ("days", "int not null", "参与计算的截面日数；低于 MIN_DAYS 时 rank_ic 为 null"),
    ("avg_universe", "numeric(10, 1)", "日均横截面宽度（过流动性门槛的标的数）"),
    ("rank_ic", "numeric(10, 6)", "逐日秩相关均值。A 股日频 |IC| 0.02~0.05 即有实用价值"),
    ("ic_std", "numeric(10, 6)", "逐日 IC 的标准差"),
    ("ic_ir", "numeric(10, 4)", "rank_ic/ic_std。比 IC 绝对值更重要——飘忽的因子下不了注"),
    ("positive_ratio", "numeric(6, 2)", "IC 为正的日子占比。45~55 视为无方向性"),
    ("monotonicity", "numeric(10, 6)", "分位组均收益的秩相关；差则线性加权合成会失效"),
    ("verdict", "text", "样本不足 / 无方向性 / 正向·可用 / 反向·可用 / 正向·偏弱 / 反向·偏弱"),
    ("useful", "boolean not null default false", "是否值得进合成模型"),
    ("sign", "smallint not null default 0", "合成方向：+1 直接用，-1 取负，0 不用"),
    ("suggested_weight", "numeric(10, 6)", "该次运行建议的合成权重（按 |ic_ir| 归一化并带方向）"),
    ("created_at", "timestamptz not null default now()", "写入时间"),
)

UNIQUE_KEY = ("eval_date", "factor_name", "horizon", "segment")


def build_ddl() -> str:
    """生成完整建表语句。幂等——可重复执行。"""
    body = ["    id bigserial primary key,"]
    for name, ddl_type, comment in COLUMNS:
        body.append(f"    -- {comment}")
        body.append(f"    {name} {ddl_type},")
    body.append(f"    constraint {TABLE_FACTOR_IC_DAILY}_unique unique ({', '.join(UNIQUE_KEY)})")
    lines = [
        f"create table if not exists public.{TABLE_FACTOR_IC_DAILY} (",
        *body,
        ");",
        "",
        "-- 主查询：看某因子的方向随时间怎么变。",
        f"create index if not exists {TABLE_FACTOR_IC_DAILY}_factor_idx",
        f"    on public.{TABLE_FACTOR_IC_DAILY} (factor_name, horizon, eval_date desc);",
        "",
        "-- 次查询：本周有哪些可用因子。",
        f"create index if not exists {TABLE_FACTOR_IC_DAILY}_useful_idx",
        f"    on public.{TABLE_FACTOR_IC_DAILY} (eval_date desc, useful)",
        "    where useful = true;",
        "",
        f"comment on table public.{TABLE_FACTOR_IC_DAILY} is",
        "    '因子 IC 评估逐周留档。判读看 ic_ir 与跨期方向一致性，不看单期 rank_ic。';",
    ]
    return "\n".join(lines)


def payload_keys() -> frozenset[str]:
    """save_factor_ic_rows 应写入的字段（不含自增 id 与默认 created_at）。"""
    return frozenset(name for name, _, _ in COLUMNS if name != "created_at")
