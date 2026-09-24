"""已登录用户读取影子账本橱窗/详账的 SQL RPC。由 print_shadow_ledger_ddl 一并打印。"""

PAYLOAD_RPC_DDL = r"""
create schema if not exists private;

create or replace function private.shadow_sector_tag(p_name text)
returns text
language sql
immutable
as $$
  select case
    when p_name ~ '化肥|磷肥|钾肥|氮肥|复合肥' then '化肥'
    when p_name ~ '化工|化学|石化|氯碱' then '化工'
    when p_name ~ '传媒|影视|广告|出版|游戏|动漫' then '传媒'
    when p_name ~ '银行' then '银行'
    when p_name ~ '证券|券商' then '证券'
    when p_name ~ '保险' then '保险'
    when p_name ~ '半导体|芯片|集成电路|硅片' then '半导体'
    when p_name ~ '新能源|光伏|锂电|电池|储能' then '新能源'
    when p_name ~ '医药|生物|制药|医疗' then '医药'
    when p_name ~ '白酒' then '白酒'
    when p_name ~ '食品|饮料|乳业' then '食品'
    when p_name ~ '汽车|整车|汽配' then '汽车'
    when p_name ~ '煤炭|焦煤' then '煤炭'
    when p_name ~ '钢铁|特钢' then '钢铁'
    when p_name ~ '有色' then '有色'
    when p_name ~ '地产|房地产' then '地产'
    when p_name ~ '电力|电网|火电|水电' then '电力'
    when p_name ~ '军工|航空|航天' then '军工'
    when p_name ~ '计算机|软件|互联网' then '计算机'
    when p_name ~ '农业|种业|饲料' then '农业'
    when p_name ~ '电子|光电' then '电子'
    when p_name ~ '机械|装备' then '机械'
    else '其他'
  end;
$$;

create or replace function private.shadow_ledger_payload(p_as_of text default null)
returns jsonb
language plpgsql
stable
security definer
set search_path = public
as $$
declare
  v_account_id constant text := 'USER_SHADOW:e66942b7-be66-46fe-95ed-ebc7f3b47928';
  v_user uuid := auth.uid();
  v_member boolean := false;
  v_initial numeric := 100000;
  v_as_of text;
  v_pnl numeric := 0;
  v_dd numeric := 0;
  v_peak numeric := 0;
  v_eq numeric;
  v_nav jsonb := '[]'::jsonb;
  v_open int := 0;
  v_tags text[] := '{}';
  v_showcase jsonb;
  v_ledger jsonb;
  v_day date;
begin
  if v_user is null then
    raise exception 'not authenticated' using errcode = '28000';
  end if;

  select exists (
    select 1
    from planet_members pm
    where pm.user_id = v_user::text
      and (pm.expires_on is null or pm.expires_on >= (timezone('Asia/Shanghai', now()))::date)
  ) into v_member;

  select coalesce(a.initial_capital, 100000)
  into v_initial
  from shadow_account a
  where a.account_id = v_account_id;

  v_initial := coalesce(v_initial, 100000);

  for v_eq in
    select n.equity::numeric
    from shadow_nav_daily n
    where n.account_id = v_account_id
    order by n.as_of
  loop
    if v_eq > v_peak then
      v_peak := v_eq;
    end if;
    if v_peak > 0 then
      v_dd := least(v_dd, (v_eq - v_peak) / v_peak);
    end if;
  end loop;

  select n.pnl_total::numeric, to_char(n.as_of, 'YYYY-MM-DD')
  into v_pnl, v_as_of
  from shadow_nav_daily n
  where n.account_id = v_account_id
  order by n.as_of desc
  limit 1;

  select coalesce(jsonb_agg(
    jsonb_build_object(
      'asOf', to_char(n.as_of, 'YYYY-MM-DD'),
      'nav', case when v_initial > 0 then n.equity / v_initial else 0 end
    )
    order by n.as_of
  ), '[]'::jsonb)
  into v_nav
  from shadow_nav_daily n
  where n.account_id = v_account_id;

  select count(*)::int
  into v_open
  from shadow_positions p
  where p.account_id = v_account_id and p.shares > 0;

  select coalesce(array_agg(distinct private.shadow_sector_tag(p.name) order by private.shadow_sector_tag(p.name)), '{}')
  into v_tags
  from shadow_positions p
  where p.account_id = v_account_id and p.shares > 0;

  v_showcase := jsonb_build_object(
    'navCurve', v_nav,
    'periodPnlAmount', coalesce(v_pnl, 0),
    'periodPnlPct', case when v_initial > 0 then coalesce(v_pnl, 0) / v_initial * 100 else 0 end,
    'maxDrawdownPct', abs(v_dd) * 100,
    'winRatePct', null,
    'winRateNote', '平仓样本不足，未计算粗胜率',
    'openPositionCount', v_open,
    'sectorTags', to_jsonb(coalesce(v_tags, '{}'))
  );

  if not v_member then
    return jsonb_build_object(
      'tier', 'showcase',
      'accountId', v_account_id,
      'asOf', v_as_of,
      'disclaimer', '策略按威科夫漏斗跑纸面账；非投资建议',
      'showcase', v_showcase
    );
  end if;

  begin
    v_day := nullif(btrim(coalesce(p_as_of, '')), '')::date;
  exception
    when others then
      v_day := null;
  end;

  select jsonb_build_object(
    'navDaily', coalesce((
      select jsonb_agg(jsonb_build_object(
        'asOf', to_char(n.as_of, 'YYYY-MM-DD'),
        'cash', n.cash,
        'marketValue', n.market_value,
        'equity', n.equity,
        'pnlDay', n.pnl_day,
        'pnlTotal', n.pnl_total
      ) order by n.as_of)
      from shadow_nav_daily n
      where n.account_id = v_account_id
    ), '[]'::jsonb),
    'events', coalesce((
      select jsonb_agg(jsonb_build_object(
        'asOf', to_char(e.as_of, 'YYYY-MM-DD'),
        'code', e.code,
        'name', e.name,
        'eventType', e.event_type,
        'price', e.price,
        'qty', e.qty,
        'reason', e.reason,
        'fees', coalesce(e.fees, '{}'::jsonb)
      ) order by e.as_of, e.code)
      from shadow_events e
      where e.account_id = v_account_id
        and (v_day is null or e.as_of = v_day)
    ), '[]'::jsonb),
    'positions', coalesce((
      select jsonb_agg(jsonb_build_object(
        'code', p.code,
        'name', p.name,
        'shares', p.shares,
        'avgCost', p.avg_cost,
        'lastMark', coalesce(nullif(p.last_mark, 0), p.avg_cost),
        'netPnlAmount', p.shares * coalesce(nullif(p.last_mark, 0), p.avg_cost) - p.shares * p.avg_cost,
        'netPnlPct', case
          when p.shares * p.avg_cost > 0
            then (p.shares * coalesce(nullif(p.last_mark, 0), p.avg_cost) - p.shares * p.avg_cost)
              / (p.shares * p.avg_cost) * 100
          else null
        end
      ) order by p.code)
      from shadow_positions p
      where p.account_id = v_account_id and p.shares > 0
    ), '[]'::jsonb)
  )
  into v_ledger;

  return jsonb_build_object(
    'tier', 'full',
    'accountId', v_account_id,
    'asOf', v_as_of,
    'disclaimer', '策略按威科夫漏斗跑纸面账；非投资建议',
    'showcase', v_showcase,
    'ledger', v_ledger
  );
end;
$$;

create or replace function public.shadow_ledger_payload(p_as_of text default null)
returns jsonb
language sql
stable
security definer
set search_path = public
as $$
  select private.shadow_ledger_payload(p_as_of);
$$;

revoke all on function public.shadow_ledger_payload(text) from public, anon;
grant execute on function public.shadow_ledger_payload(text) to authenticated;
revoke all on function private.shadow_ledger_payload(text) from public, anon, authenticated;
revoke all on function private.shadow_sector_tag(text) from public, anon, authenticated;
"""
