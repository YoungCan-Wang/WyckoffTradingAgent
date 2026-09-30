"""DDL helpers for portfolio write RLS that also require active planet membership."""

from __future__ import annotations

# Keep in sync with web/packages/shared/src/planet-membership.ts:
# expires_on NULL/empty → active; else YYYY-MM-DD >= Asia/Shanghai today.
_ACTIVE_MEMBER = """\
exists (
    select 1
    from public.planet_members pm
    where pm.user_id = auth.uid()::text
      and (
        pm.expires_on is null
        or pm.expires_on >= (timezone('Asia/Shanghai', now()))::date
      )
  )"""

_OWN_PORTFOLIO = "split_part(portfolio_id, ':', 2) = auth.uid()::text"
_WRITE_CHECK = f"({_OWN_PORTFOLIO}\n    and {_ACTIVE_MEMBER})"
_SELECT_CHECK = f"({_OWN_PORTFOLIO})"
_TABLES = ("portfolios", "portfolio_positions")


def build_portfolio_write_rls_ddl() -> str:
    """Return SQL that retightens INSERT/UPDATE/DELETE; SELECT stays ownership-only."""
    sections = [
        "-- Portfolio write RLS: ownership + active planet_members",
        "-- SELECT stays ownership-only; service_role still bypasses RLS.",
        "begin;",
        _drop_write_policies_block(),
    ]
    for table in _TABLES:
        sections.append(_ensure_select_policy(table))
        sections.extend(_write_policies(table))
    sections.append("commit;")
    return "\n\n".join(sections) + "\n"


def _drop_write_policies_block() -> str:
    return """\
do $$
declare item record;
begin
  for item in
    select policyname, tablename, cmd
    from pg_policies
    where schemaname = 'public'
      and tablename in ('portfolios', 'portfolio_positions')
      and cmd in ('INSERT', 'UPDATE', 'DELETE', 'ALL')
  loop
    execute format('drop policy if exists %I on public.%I', item.policyname, item.tablename);
  end loop;
end $$;"""


def _ensure_select_policy(table: str) -> str:
    # If an ALL policy was dropped above, recreate ownership-only SELECT.
    return f"""\
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public'
      and tablename = '{table}'
      and cmd = 'SELECT'
  ) then
    execute $policy$
      create policy {table}_select_own
        on public.{table}
        for select
        to authenticated
        using {_SELECT_CHECK};
    $policy$;
  end if;
end $$;"""


def _write_policies(table: str) -> list[str]:
    return [
        f"""\
create policy {table}_insert_own_member
  on public.{table}
  for insert
  to authenticated
  with check {_WRITE_CHECK};""",
        f"""\
create policy {table}_update_own_member
  on public.{table}
  for update
  to authenticated
  using {_WRITE_CHECK}
  with check {_WRITE_CHECK};""",
        f"""\
create policy {table}_delete_own_member
  on public.{table}
  for delete
  to authenticated
  using {_WRITE_CHECK};""",
    ]
