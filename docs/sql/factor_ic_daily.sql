-- 因子 IC 评估结果落库。
--
-- 为什么需要落库：IC 的单期值不构成证据，关键是 ic_ir（稳定性）与跨期方向一致性。
-- 2026-08-22 首轮扫描发现 19 个因子-前瞻组合在 3 段样本上方向全一致且全部为负——
-- 这是当日唯一跨段稳定的结论（同期参数网格的 walk-forward 仅 1/16 个窗口为正）。
-- 逐周留档才能看出「哪些因子的方向在变」，而这正是决定能否替换八通道阈值门的依据。
--
-- 在 Supabase SQL Editor 执行。Python SDK 走 REST、不支持 DDL，故需人工跑一次。

create table if not exists public.factor_ic_daily (
    id bigserial primary key,
    -- 评估运行日（非行情日）。同一 run 的所有因子共享，便于按周对比。
    eval_date date not null,
    -- 因子名，与 scripts/scan_factor_ic.py 的 build_factors 键一致。
    factor_name text not null,
    -- 前瞻交易日数（5 / 10）。
    horizon int not null,
    -- 评估分段：full = 全样本；seg1/seg2/... = 滚动分段。
    segment text not null default 'full',
    -- 行情区间，便于复现。
    window_start date,
    window_end date,
    -- 参与计算的截面日数量；低于 core.factor_ic.MIN_DAYS 时 rank_ic 为 null。
    days int not null,
    -- 日均横截面宽度（过流动性门槛的标的数）。
    avg_universe numeric(10, 1),
    -- 逐日 Spearman 秩相关的均值。A 股日频 |IC| 0.02~0.05 即有实用价值。
    rank_ic numeric(10, 6),
    ic_std numeric(10, 6),
    -- rank_ic / ic_std。比 IC 绝对值更重要——IC 高但飘忽的因子无法下注。
    ic_ir numeric(10, 4),
    -- IC 为正的日子占比。45~55 视为无方向性（与 AGENTS.md 噪声判定一致）。
    positive_ratio numeric(6, 2),
    -- 分位组均收益的秩相关，衡量线性程度；差则加权合成会失效。
    monotonicity numeric(10, 6),
    -- 判定文案：样本不足 / 无方向性 / 正向·可用 / 反向·可用 / 正向·偏弱 / 反向·偏弱
    verdict text,
    -- 是否值得进合成模型（|IC|>=0.02 且 |IC_IR|>=0.30 且非无方向性）。
    useful boolean not null default false,
    -- 合成方向：+1 直接用，-1 取负后用，0 不用。
    sign smallint not null default 0,
    -- 该次运行建议的合成权重（按 |ic_ir| 归一化并带方向），未入选为 null。
    suggested_weight numeric(10, 6),
    created_at timestamptz not null default now(),
    -- 同一 (eval_date, factor, horizon, segment) 只保留一条，重跑覆盖。
    constraint factor_ic_daily_unique unique (eval_date, factor_name, horizon, segment)
);

-- 主查询模式是「看某因子的方向随时间怎么变」。
create index if not exists factor_ic_daily_factor_idx
    on public.factor_ic_daily (factor_name, horizon, eval_date desc);

-- 次查询模式是「本周有哪些可用因子」。
create index if not exists factor_ic_daily_useful_idx
    on public.factor_ic_daily (eval_date desc, useful)
    where useful = true;

comment on table public.factor_ic_daily is
    '因子 IC 评估逐周留档。判读看 ic_ir 与跨期方向一致性，不看单期 rank_ic。';
