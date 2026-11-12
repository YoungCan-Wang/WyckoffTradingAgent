"""Market sentiment filter, 60-day overhead-supply check, and Feishu sniper push for limit-up-hunter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[4]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utils.feishu_report_card import build_report_card_elements, report_card_template  # noqa: E402


@dataclass(frozen=True)
class MarketSentiment:
    red_count: int
    green_count: int
    sealed_lu_count: int
    bull_trap_count: int

    @property
    def trap_to_lu_ratio(self) -> float:
        return self.bull_trap_count / max(self.sealed_lu_count, 1)

    @property
    def is_trap_wave(self) -> bool:
        return self.trap_to_lu_ratio >= 1.35 and self.green_count >= int(self.red_count * 1.25)

    def format_banner(self) -> str:
        mode = (
            "🌩️早盘冲高派面潮·严禁左侧伸手(仅看抗分歧过早高真活口)"
            if self.is_trap_wave
            else "☀️情绪正常/接力活跃·可贴均线低吸主线前排"
        )
        return (
            f"红盘:{self.red_count}家 | 绿盘:{self.green_count}家 | 涨停封死:{self.sealed_lu_count}家 "
            f"| 冲高回落派面:{self.bull_trap_count}家 (派面/封板比:{self.trap_to_lu_ratio:.2f}x) -> [{mode}]"
        )


@dataclass(frozen=True)
class SniperCandidate:
    symbol: str
    name: str
    last_price: float
    last_pct: float
    vwap: float
    limit_up_price: float
    target_buy_low: float
    target_buy_high: float
    defense_price: float
    themes: tuple[str, ...]
    daily_tag: str
    five_min_tag: str
    amount_yi: float


def compute_market_sentiment(metrics: list[Any]) -> MarketSentiment:
    red_c = sum(1 for m in metrics if m.last_pct > 0)
    green_c = sum(1 for m in metrics if m.last_pct < 0)
    lu_c = sum(1 for m in metrics if m.is_limit_up)
    trap_c = sum(1 for m in metrics if not m.is_limit_up and m.high_pct >= 6.0 and m.drop_from_high <= -4.0)
    return MarketSentiment(red_c, green_c, lu_c, trap_c)


def evaluate_daily_overhead(daily_rows: list[dict[str, float]], last_price: float) -> tuple[bool, str]:
    if not daily_rows or last_price <= 0:
        return True, "[日线未校验]"
    ordered = sorted(daily_rows, key=lambda r: str(r.get("trade_date", "")))
    high_60d = max(float(r.get("high") or 0.0) for r in ordered)
    recent_15 = ordered[-15:]
    high_15d = max(float(r.get("high") or 0.0) for r in recent_15)
    big_dumps_15d = sum(1 for r in recent_15 if float(r.get("pct_chg") or 0.0) <= -8.0)
    recent_5 = ordered[-5:]
    big_dumps_5d = sum(1 for r in recent_5 if float(r.get("pct_chg") or 0.0) <= -6.5)

    if big_dumps_5d >= 1 or (big_dumps_15d >= 1 and high_15d >= last_price * 1.08):
        return False, "[💣近15日有大阴跌停套牢盘·剔除]"
    if high_60d >= last_price * 1.22 and high_15d >= last_price * 1.12:
        return False, "[💣上方60日套牢盘厚重·剔除]"
    if last_price >= high_60d * 0.995:
        return True, "[🚀突破60日箱体新高·上方零套牢]"
    lu_10d = sum(1 for r in ordered[-10:] if float(r.get("pct_chg") or 0.0) >= 9.6)
    if lu_10d >= 2 and last_price >= high_15d * 0.94:
        return True, "[🔥人气连板强势洗盘·二波起跳区]"
    if lu_10d >= 1 and last_price >= high_15d * 0.93:
        return True, "[🔥N字首板揉搓·二波起跳区]"
    return True, "[✅日线结构干净·无密集大阴套牢]"


def fetch_daily_overhead_map(
    symbols: list[str],
    snapshot_date: str,
    tushare_token: str | None,
) -> dict[str, list[dict[str, float]]]:
    if not symbols or not tushare_token:
        return {}
    try:
        import tushare as ts

        pro = ts.pro_api(tushare_token)
        out: dict[str, list[dict[str, float]]] = {}
        for sym in symbols[:18]:
            df = pro.daily(ts_code=sym, end_date=snapshot_date, limit=60)
            if df is None or df.empty:
                continue
            rows = [
                {
                    "trade_date": str(r.trade_date),
                    "high": float(r.high),
                    "low": float(r.low),
                    "pct_chg": float(r.pct_chg),
                }
                for r in df.itertuples(index=False)
                if str(r.trade_date) < snapshot_date
            ]
            if rows:
                out[sym] = rows
        return out
    except Exception:
        return {}


def _passes_sniper_bar_gate(m: Any, sentiment: MarketSentiment, now_hhmm: str, reversal_risk_fn: Any) -> bool:
    fm = m.five_min
    if not fm or not fm.held_first_5m_low:
        return False
    if fm.first_5m_surge_pct >= 3.0:
        if fm.min_vol_ratio > 0.25:
            return False
    elif not fm.broke_morning_high:
        return False
    if reversal_risk_fn and ("🚨" in reversal_risk_fn(m) or "⚠️" in reversal_risk_fn(m)):
        return False
    if sentiment.is_trap_wave and now_hhmm < "10:40":
        if not fm.broke_morning_high or m.drop_from_high < -1.2 or m.amount_yi < 1.5:
            return False
    return True


def select_sniper_targets(
    metrics: list[Any],
    themes: list[dict[str, Any]],
    sentiment: MarketSentiment,
    now_hhmm: str,
    overhead_rows: dict[str, list[dict[str, float]]] | None = None,
    max_price: float = 15.0,
    reversal_risk_fn: Any = None,
) -> list[SniperCandidate]:
    theme_scores = {t["theme"]: int(t.get("echelon_score", 0)) for t in themes if t.get("lu_count", 0) >= 3}
    shortlist: list[tuple[Any, tuple[str, ...], int]] = []
    for m in metrics:
        if (
            m.symbol.startswith(("30", "68"))
            or m.is_limit_up
            or m.reg_risk_tag
            or not getattr(m, "has_monster_gene", True)
        ):
            continue
        if m.last_price > max_price + 1e-4 or not m.above_vwap or m.amount_yi < 0.6:
            continue
        vwap_gap = (m.last_price - m.vwap) / m.vwap if m.vwap > 0 else 1.0
        if not (2.5 <= m.last_pct <= 7.2 and 0.0 <= vwap_gap <= 0.025):
            continue
        if not _passes_sniper_bar_gate(m, sentiment, now_hhmm, reversal_risk_fn):
            continue
        hit_themes = tuple(c for c in m.concepts if c in theme_scores)
        is_promo = m.prev_boards >= 1 or bool(m.m_days_n_boards)
        if not hit_themes and not is_promo:
            continue
        best_ech = max((theme_scores[th] for th in hit_themes), default=0)
        if hit_themes and best_ech < 0 and not is_promo:
            continue
        shortlist.append((m, hit_themes, best_ech))

    shortlist.sort(
        key=lambda item: (
            item[2],
            getattr(item[0], "monster_tier", 1),
            item[0].prev_boards >= 1,
            len(item[1]),
            item[0].last_price <= 10.0,
            item[0].five_min.first_5m_surge_pct if item[0].five_min else item[0].high_pct,
        ),
        reverse=True,
    )
    results: list[SniperCandidate] = []
    for m, hit_themes, _ in shortlist[:6]:
        ok_daily, d_tag = evaluate_daily_overhead((overhead_rows or {}).get(m.symbol, []), m.last_price)
        if not ok_daily:
            continue
        buy_low = round(m.vwap, 2)
        buy_high = round(max(buy_low, min(m.last_price, m.vwap * 1.015)), 2)
        f_low = m.five_min.first_5m_low if m.five_min and m.five_min.first_5m_low > 0 else m.low_price
        defense = round(max(f_low, m.vwap * 0.975), 2)
        results.append(
            SniperCandidate(
                symbol=m.symbol,
                name=m.name,
                last_price=m.last_price,
                last_pct=m.last_pct,
                vwap=m.vwap,
                limit_up_price=m.limit_up_price,
                target_buy_low=buy_low,
                target_buy_high=buy_high,
                defense_price=defense,
                themes=hit_themes or (m.ladder_label,),
                daily_tag=d_tag,
                five_min_tag=m.five_min.format_tag() if m.five_min else "",
                amount_yi=m.amount_yi,
            )
        )
    return results


def resolve_lark_open_id(explicit_id: str | None = None) -> str:
    if explicit_id and explicit_id.strip():
        return explicit_id.strip()
    env_id = os.environ.get("FEISHU_USER_OPEN_ID", "").strip()
    if env_id:
        return env_id
    res = subprocess.run(["lark-cli", "auth", "status"], capture_output=True, text=True, check=False)
    if res.returncode == 0 and res.stdout.strip():
        try:
            data = json.loads(res.stdout)
            return str(((data.get("identities") or {}).get("user") or {}).get("openId") or "").strip()
        except Exception:
            return ""
    return ""


def build_sniper_card_markdown(
    targets: list[SniperCandidate],
    sentiment: MarketSentiment,
    snap_time: str,
    ladder_brief: list[str] | None = None,
) -> tuple[str, str]:
    top = targets[0]
    title = (
        f"🎯【抓妖狙击】{top.name}({top.symbol[:6]}) "
        f"目标价:{top.target_buy_low:.2f}~{top.target_buy_high:.2f}元 | 防守:{top.defense_price:.2f}元"
    )
    lines = [
        f"**⏱️ 扫描时刻**：`{snap_time}`",
        f"**🌡️ 市场温度**：{sentiment.format_banner()}",
    ]
    if ladder_brief:
        lines.append("**🪜 连板晋级生态**：\n" + "\n".join(f"- {row}" for row in ladder_brief[:4]))
    lines.append("---")
    for idx, c in enumerate(targets[:3], 1):
        lines.extend(
            [
                f"### {idx}. 🔥 {c.name}（`{c.symbol}`）— 现价 `{c.last_price:.2f}元 ({c.last_pct:+.2f}%)`",
                f"- **🎯 建议低吸目标价**：**`{c.target_buy_low:.2f} ~ {c.target_buy_high:.2f}元`**（贴均线 `VWAP {c.vwap:.2f}元`）",
                f"- **🚀 日内冲板目标价**：`{c.limit_up_price:.2f}元` | **🛡️ 破位防守价**：**`{c.defense_price:.2f}元`**",
                f"- **🧭 共振主线**：`{' + '.join(c.themes[:4])}` | 成交 `{c.amount_yi:.2f}亿`",
                f"- **📊 结构验钞**：`{c.daily_tag}` `{c.five_min_tag}`",
            ]
        )
    return title, "\n".join(lines)


def push_sniper_feishu_alert(
    targets: list[SniperCandidate],
    sentiment: MarketSentiment,
    snap_date: str,
    snap_time: str,
    level: str = "L2",
    user_open_id: str | None = None,
    dry_run: bool = False,
    ladder_brief: list[str] | None = None,
) -> str | None:
    cache_dir = Path.home() / ".wyckoff" / "cache"
    sent_file = cache_dir / f"hunter_feishu_sent_{snap_date}.json"
    already_sent: set[str] = set()
    if sent_file.exists():
        try:
            already_sent = set(json.loads(sent_file.read_text(encoding="utf-8")))
        except Exception:
            already_sent = set()

    fresh = [c for c in targets if c.symbol not in already_sent]
    if not fresh:
        return None
    title, content = build_sniper_card_markdown(fresh, sentiment, snap_time, ladder_brief=ladder_brief)
    if dry_run:
        print(f"[DRY-RUN FEISHU {level}] {title}\n{content}")
        return "dry_run_msg_id"

    oid = resolve_lark_open_id(user_open_id)
    if not oid:
        print("[WARN] 未检测到飞书 open_id，跳过飞书推送。", file=sys.stderr)
        return None

    card_payload = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": report_card_template(title, content),
        },
        "elements": build_report_card_elements(content),
    }
    res = subprocess.run(
        [
            "lark-cli",
            "im",
            "+messages-send",
            "--as",
            "bot",
            "--user-id",
            oid,
            "--msg-type",
            "interactive",
            "--content",
            json.dumps(card_payload, ensure_ascii=False),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    msg_id = None
    if res.returncode == 0 and res.stdout.strip():
        try:
            rj = json.loads(res.stdout)
            msg_id = (rj.get("data") or {}).get("message_id") or rj.get("message_id")
        except Exception:
            msg_id = None
    if not msg_id:
        print(f"[ERROR] 飞书推送失败: {res.stderr.strip()}", file=sys.stderr)
        return None

    urgent_map = {"L2": "urgent_app", "L3": "urgent_sms", "L4": "urgent_phone"}
    if level in urgent_map:
        subprocess.run(
            [
                "lark-cli",
                "im",
                "messages",
                urgent_map[level],
                "--as",
                "bot",
                "--params",
                json.dumps({"message_id": msg_id, "user_id_type": "open_id"}, ensure_ascii=False),
                "--data",
                json.dumps({"user_id_list": [oid]}, ensure_ascii=False),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    cache_dir.mkdir(parents=True, exist_ok=True)
    already_sent.update(c.symbol for c in fresh[:3])
    sent_file.write_text(json.dumps(sorted(already_sent), ensure_ascii=False), encoding="utf-8")
    return str(msg_id)
