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

仓库目前没有维护 Supabase migration history，因此不要把执行一次后失效的 SQL 文件提交进仓库。发布负责人在 Supabase SQL Editor 中执行下面的受控迁移，并把执行记录留在发布单中。

这是一次明确的 breaking cutover，不创建旧表视图、不保留旧字段，也不支持旧 `/guide` 路由。上线顺序固定为：

1. 合并并完成 Worker/Web 部署；此时新代码查询尚不存在的 `planet_members`，会员校验失败关闭，会员能力会暂时不可用；
2. 验证新提交的 Worker health、Pages 和普通用户能力正常；数据库尚未修改，此时仍可安全回滚应用；
3. 立即执行下方数据库事务；成功后会员能力恢复；
4. 验证五条会员鉴权路径。数据库迁移完成后不得单独回滚到旧应用，必须前向修复或同时反向迁移数据库。

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

do $$
declare
  item record;
begin
  for item in
    select policyname
    from pg_policies
    where schemaname = 'public'
      and tablename = 'planet_members'
  loop
    execute format('drop policy %I on public.planet_members', item.policyname);
  end loop;
end $$;

alter table public.planet_members enable row level security;
revoke all on table public.planet_members from anon;
revoke all on table public.planet_members from authenticated;
grant select on table public.planet_members to authenticated;

create policy planet_members_select_own
  on public.planet_members
  for select
  to authenticated
  using (user_id = (select auth.uid())::text);

notify pgrst, 'reload schema';

commit;
```

迁移后立即验证：

1. 用普通登录账号读取 `planet_members`，结果为空且会员页显示未开通；
2. 用有效会员账号只能读取自己的记录，会员页显示到期日或长期有效；
3. 验证跟踪、归因、云端持仓、沙箱、手机遥控五条鉴权路径；
4. 确认 PostgREST 中不存在 `whitelist` 表或视图，旧 `/guide` 返回 SPA 的未匹配路由结果而不会跳转。

如果数据库迁移后必须回滚，应用和数据库要在同一个维护窗口一起退回：

```sql
begin;

drop policy if exists planet_members_select_own on public.planet_members;
alter table public.planet_members
  alter column expires_on type text
  using case
    when expires_on is null then null
    else to_char(expires_on, 'YYYYMMDD')
  end;
alter table public.planet_members rename column expires_on to expire_date;
alter table public.planet_members rename to whitelist;

do $$
begin
  if exists (
    select 1
    from pg_constraint
    where conrelid = 'public.whitelist'::regclass
      and conname = 'planet_members_pkey'
  ) then
    alter table public.whitelist
      rename constraint planet_members_pkey to whitelist_pkey;
  end if;
end $$;

revoke all on table public.whitelist from anon;
revoke all on table public.whitelist from authenticated;
grant select on table public.whitelist to authenticated;
create policy whitelist_select_own
  on public.whitelist
  for select
  to authenticated
  using (user_id = (select auth.uid())::text);

notify pgrst, 'reload schema';
commit;
```
