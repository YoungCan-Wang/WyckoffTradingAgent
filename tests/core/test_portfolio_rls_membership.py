"""Portfolio write RLS must require active planet membership, not ownership alone."""

from __future__ import annotations

from core.portfolio_rls_membership import build_portfolio_write_rls_ddl


def test_portfolio_write_rls_ddl_requires_membership_and_ownership():
    ddl = build_portfolio_write_rls_ddl()
    assert "planet_members" in ddl
    assert "Asia/Shanghai" in ddl
    assert "split_part(portfolio_id, ':', 2) = auth.uid()::text" in ddl
    assert "portfolios_insert_own_member" in ddl
    assert "portfolio_positions_update_own_member" in ddl
    assert "portfolio_positions_delete_own_member" in ddl
    assert "for select" in ddl.lower() or "select_own" in ddl
    assert "service_role" in ddl
    # Write paths must not leave membership out of WITH CHECK / USING.
    assert ddl.count("expires_on is null") >= 3
