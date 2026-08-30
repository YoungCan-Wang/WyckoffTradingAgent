# 星球会员

“星球会员”是产品名称，也是代码和数据库中的唯一业务术语。`whitelist`、`isWhitelisted` 等旧名称不再用于会员鉴权；安全上游、字段集合等通用 allowlist 概念不受影响。

## 能力边界

普通登录用户可以自行配置模型和数据源，使用读盘室、单股分析、多股对抗、手动持仓诊断、数据导出和浏览器本地历史。

有效星球会员在此基础上增加：

- 最近 30 个复盘交易日的形态跟踪；
- 策略归因与信号分层报告；
- 云端持仓保存和多端同步；
- 经用户确认的隔离 Python 研究计算；
- 手机遥控桌面读盘室；
- 每日漏斗产物和星球交流服务。

会员身份只表示共享能力的访问权，不自动给账号写入私人模型 Key、TickFlow Key 或通知地址。页面会把“会员有效”和“模型/数据源已配置”作为两件事分别展示。会员服务不是投资顾问服务，也不承诺收益。

## 数据契约

`public.planet_members` 是会员身份的唯一事实表：

| 字段 | 类型 | 语义 |
| --- | --- | --- |
| `user_id` | `text` | Supabase Auth 用户 ID，主键 |
| `created_at` | `timestamptz` | 绑定时间 |
| `expires_on` | `date` | 最后一个有效自然日；`NULL` 表示长期有效 |

客户端只能按 `auth.uid()` 读取自己的会员记录。会员的新增、延期和撤销由受信服务端或数据库管理员完成，客户端没有写权限。

## 线上改名与发布顺序

仓库目前没有维护 Supabase migration history，因此不要把执行一次后失效的 SQL 文件提交进仓库。发布负责人在 Supabase SQL Editor 中执行下面的受控迁移，并把执行记录留在发布单中。表改名会保留原表的 RLS、授权和依赖；兼容视图让迁移期间的旧 Web/API 仍能读取旧字段。

执行前先确认旧数据均为 `NULL`、空字符串或合法 `YYYYMMDD`：

```sql
select expire_date, count(*)
from public.whitelist
group by expire_date
order by expire_date nulls first;

select user_id, expire_date
from public.whitelist
where nullif(btrim(expire_date), '') is not null
  and (
    expire_date !~ '^\d{8}$'
    or to_char(to_date(expire_date, 'YYYYMMDD'), 'YYYYMMDD') <> expire_date
  );

select policyname, roles, cmd, qual
from pg_policies
where schemaname = 'public' and tablename = 'whitelist';
```

确认后执行：

```sql
begin;

lock table public.whitelist in access exclusive mode;
alter table public.whitelist rename to planet_members;
alter table public.planet_members rename column expire_date to expires_on;
alter table public.planet_members
  alter column expires_on type date
  using case
    when nullif(btrim(expires_on), '') is null then null
    else to_date(expires_on, 'YYYYMMDD')
  end;

do $$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.planet_members'::regclass
      and conname = 'whitelist_pkey'
  ) then
    alter table public.planet_members
      rename constraint whitelist_pkey to planet_members_pkey;
  end if;
end $$;

alter table public.planet_members enable row level security;
revoke all on table public.planet_members from anon;
grant select on table public.planet_members to authenticated;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'planet_members'
      and policyname = 'planet_members_select_own'
  ) then
    create policy planet_members_select_own
      on public.planet_members
      for select
      to authenticated
      using (user_id = (select auth.uid())::text);
  end if;
end $$;

create view public.whitelist
with (security_invoker = true)
as
select
  user_id,
  created_at,
  to_char(expires_on, 'YYYYMMDD') as expire_date
from public.planet_members;

revoke all on table public.whitelist from anon;
grant select on table public.whitelist to authenticated;
notify pgrst, 'reload schema';

commit;
```

迁移后按顺序验证：

1. 用普通登录账号读取 `planet_members`，结果为空且会员页显示未开通；
2. 用有效会员账号只能读取自己的记录，会员页显示到期日或长期有效；
3. 旧版本通过兼容视图仍能读取 `expire_date`；
4. 发布 Worker 和 Web，验证跟踪、归因、云端持仓、沙箱、手机遥控五条鉴权路径；
5. 观察至少一个发布周期，确认旧前端缓存和旧 Worker 已退出后再删除兼容视图：

```sql
drop view public.whitelist;
notify pgrst, 'reload schema';
```

迁移后若新版本需要回退，只需回滚应用，旧版本会继续走兼容视图；不要立即把物理表改回旧名。只有确认没有新版本流量后，才考虑在维护窗口撤销结构迁移。
