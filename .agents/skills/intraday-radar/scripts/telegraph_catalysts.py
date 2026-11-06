#!/usr/bin/env python3
"""Real-time 7x24 financial telegraph scanner and Wyckoff L2/RPS catalyst matcher."""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
SINA_7X24_URL = "https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size=40&zhibo_id=152"
WSCN_LIVE_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=25"

# 2026-10-15 换仓后的国家队/汇金 20 只重点宽基 ETF 观测篮子（按重要性与流动性排序）
NATIONAL_TEAM_20_ETFS: dict[str, str] = {
    "510310.SH": "沪深300ETF易方达",
    "588000.SH": "科创50ETF华夏",
    "159915.SZ": "创业板ETF易方达",
    "510300.SH": "沪深300ETF华泰柏瑞",
    "563360.SH": "A500ETF华泰柏瑞",
    "159352.SZ": "A500ETF南方",
    "512100.SH": "中证1000ETF南方",
    "159338.SZ": "中证A500ETF国泰",
    "510500.SH": "中证500ETF南方",
    "159949.SZ": "创业板50ETF华安",
    "588080.SH": "科创50ETF易方达",
    "510050.SH": "上证50ETF华夏",
    "510210.SH": "上证指数ETF富国",
    "159845.SZ": "中证1000ETF华夏",
    "159952.SZ": "创业板ETF广发",
    "159361.SZ": "A500ETF易方达",
    "512050.SH": "A500ETF华夏",
    "159633.SZ": "中证1000ETF易方达",
    "560010.SH": "中证1000ETF广发",
    "159919.SH": "沪深300ETF嘉实",
}

CATALYST_THEMES: list[tuple[str, list[str], list[str]]] = [
    (
        "🤖 机器人/具身智能",
        ["机器人", "具身智能", "PEEK", "减速器", "丝杠", "灵巧手", "宇树", "智元", "外骨骼"],
        ["汽车配件", "机械基件", "机床制造", "专用机械", "电器仪表"],
    ),
    (
        "🎬 AI短剧/影视传媒",
        ["短剧", "漫剧", "影视", "院线", "Seedance", "多模态", "视频生成", "票房", "游戏", "文化传媒"],
        ["影视音像", "广告包装", "出版业", "互联网", "文教休闲"],
    ),
    (
        "💻 算力/AI云/芯片",
        ["算力", "大模型", "云计算", "光模块", "芯片", "半导体", "信创", "智算", "液冷", "服务器"],
        ["软件服务", "IT设备", "半导体", "通信设备", "元器件"],
    ),
    (
        "⚡ 电网/特高压/绿电",
        ["电网", "特高压", "变压器", "算电协同", "电力设备", "虚拟电厂", "储能", "光伏", "风电", "核电"],
        ["电气设备", "新型电力", "火力发电", "水力发电"],
    ),
    (
        "🥇 有色/黄金/战略矿产",
        ["黄金", "白银", "铜", "稀土", "钨", "锑", "小金属", "有色", "锂", "钴", "铝"],
        ["黄金", "小金属", "铜", "铝", "铅锌", "矿业"],
    ),
    (
        "🧪 化工/钢铁/周期涨价",
        ["化工", "纯碱", "磷", "钛白粉", "氟化工", "制冷剂", "MDI", "煤炭", "钢铁", "水泥"],
        ["化工原料", "农药化肥", "化学制品", "煤炭开采", "普钢", "特种钢"],
    ),
    (
        "🛒 消费/免税/商业零售",
        ["免税", "离岛", "零售", "百货", "首发经济", "促消费", "内需", "猪肉"],
        ["百货", "超市连锁", "旅游服务", "食品", "服饰"],
    ),
    (
        "💊 医药/基因/医疗器械",
        ["创新药", "医疗服务", "基因", "脑机接口", "医疗器械", "集采", "减肥药", "中药"],
        ["化学制药", "生物制药", "医疗保健", "中成药"],
    ),
    (
        "🌾 农业/种业/粮食安全",
        ["种业", "种子", "粮食", "大豆", "玉米", "转基因", "农林", "耕地", "一号文件", "农产品", "关税反制", "农业"],
        ["种植业", "农业综合", "农药化肥", "饲料"],
    ),
    (
        "🏛️ 宏观/部委/关税地缘",
        ["央行", "证监会", "发改委", "商务部", "工信部", "财政部", "政治局", "国务院", "降准", "降息", "关税", "汇金"],
        ["种植业", "半导体", "证券"],
    ),
]

A_SHARE_TAPE_KEYWORDS = (
    "板块",
    "概念",
    "涨停",
    "20cm",
    "连板",
    "拉升",
    "走高",
    "走强",
    "异动",
    "A股",
    "沪指",
    "创业板",
    "科创",
    "南向资金",
    "主力资金",
)


def _clean_text(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw or "").strip()
    return re.sub(r"\s+", " ", txt)


def fetch_sina_telegraph(pages: int = 2, timeout: float = 4.0) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for page in range(1, max(pages, 1) + 1):
        try:
            req = urllib.request.Request(
                SINA_7X24_URL.format(page=page),
                headers={"User-Agent": "Mozilla/5.0 (WyckoffRadar/1.0)"},
            )
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="ignore")
            feed_list = (((json.loads(raw).get("result") or {}).get("data") or {}).get("feed") or {}).get("list") or []
            for entry in feed_list:
                ct = str(entry.get("create_time") or "")
                try:
                    dt = datetime.strptime(ct, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN_TZ)
                    ts = dt.timestamp()
                except Exception:
                    continue
                txt = _clean_text(str(entry.get("rich_text") or ""))
                if txt:
                    items.append({"id": f"sina_{entry.get('id')}", "ts": ts, "hm": dt.strftime("%H:%M"), "text": txt})
        except Exception:
            break
    return items


def fetch_wscn_telegraph(timeout: float = 4.0) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    try:
        req = urllib.request.Request(WSCN_LIVE_URL, headers={"User-Agent": "Mozilla/5.0 (WyckoffRadar/1.0)"})
        raw = urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", errors="ignore")
        rows = ((json.loads(raw).get("data") or {}).get("items")) or []
        for entry in rows:
            ts = float(entry.get("display_time") or 0.0)
            if ts <= 0:
                continue
            dt = datetime.fromtimestamp(ts, tz=CN_TZ)
            title = _clean_text(str(entry.get("title") or ""))
            body = _clean_text(str(entry.get("content_text") or entry.get("content") or ""))
            txt = f"【{title}】{body}" if title and title not in body else (body or title)
            if txt:
                items.append({"id": f"wscn_{entry.get('id')}", "ts": ts, "hm": dt.strftime("%H:%M"), "text": txt})
    except Exception:
        return []
    return items


def _rank_wyckoff_picks(
    target_sectors: list[str],
    l2_map: dict[str, dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
) -> list[str]:
    scored: list[tuple[float, str]] = []
    for code6, meta in l2_map.items():
        if meta.get("sector") not in target_sectors:
            continue
        is_l2 = bool(meta.get("l2_eligible"))
        is_cand = bool(meta.get("is_candidate"))
        rps = float(meta.get("rps") or 0.0)
        if not (is_l2 or is_cand or rps >= 88.0):
            continue
        sym = f"{code6}.SH" if code6.startswith(("6", "9")) else f"{code6}.SZ"
        q = quotes.get(sym, {})
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        has_q = last > 0 and pre > 0
        pct = ((last - pre) / pre * 100.0) if has_q else float(q.get("pct") or 0.0)
        if pct >= 9.6:
            continue
        mb_bonus = 20.0 if code6.startswith(("60", "00")) else 0.0
        l2_bonus = 30.0 if is_l2 else (15.0 if is_cand else 0.0)
        score = mb_bonus + l2_bonus + rps * 0.3 + max(min(pct, 6.0), -2.0) * 2.0
        ch_str = "/".join(meta.get("channels") or []) or ("候选" if is_cand else f"RPS{rps:.0f}")
        tag = f"{pct:+.1f}%·{ch_str}" if (has_q or "pct" in q) else ch_str
        scored.append((score, f"`{code6}` {meta.get('name', '')}({tag})"))
    scored.sort(key=lambda x: -x[0])
    return [item[1] for item in scored[:3]]


def match_theme_and_candidates(
    text: str,
    l2_map: dict[str, dict[str, Any]],
    curr_quotes: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    matched_theme = ""
    target_sectors: list[str] = []
    for theme_name, kws, sectors in CATALYST_THEMES:
        if any(k in text for k in kws):
            matched_theme = theme_name
            target_sectors = sectors
            break
    if not matched_theme and not any(k in text for k in A_SHARE_TAPE_KEYWORDS):
        return "", []
    if not matched_theme:
        matched_theme = "⚡ 盘面异动电报"
    if not target_sectors or not l2_map:
        return matched_theme, []
    return matched_theme, _rank_wyckoff_picks(target_sectors, l2_map, curr_quotes or {})


def scan_live_telegraph_catalysts(
    l2_map: dict[str, dict[str, Any]],
    curr_quotes: dict[str, dict[str, Any]] | None = None,
    since_ts: float | None = None,
    window_minutes: int = 25,
    max_items: int = 5,
    pages: int = 2,
) -> list[str]:
    cutoff = since_ts if (since_ts and since_ts > 0) else (datetime.now(CN_TZ).timestamp() - window_minutes * 60)
    raw_items = fetch_sina_telegraph(pages=pages) + fetch_wscn_telegraph()
    raw_items.sort(key=lambda x: -x["ts"])
    seen_snippets: set[str] = set()
    lines: list[str] = []
    for item in raw_items:
        if item["ts"] <= cutoff:
            continue
        txt = item["text"]
        dedup_key = re.sub(r"^【[^】]+】", "", txt)[:24]
        if dedup_key in seen_snippets:
            continue
        theme, picks = match_theme_and_candidates(txt, l2_map, curr_quotes)
        if not theme:
            continue
        seen_snippets.add(dedup_key)
        short_txt = txt[:92] + ("..." if len(txt) > 92 else "")
        pick_suffix = f" 👉 **威科夫映射**: {'、'.join(picks)}" if picks else ""
        lines.append(f"- **{item['hm']} [{theme}]** {short_txt}{pick_suffix}")
        if len(lines) >= max_items:
            break
    return lines


def summarize_national_team_etfs(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]] | None = None,
    has_baseline: bool = False,
) -> tuple[str, str]:
    prev_map = prev_q or {}
    tot_amt = 0.0
    tot_damt = 0.0
    movers: list[tuple[float, float, str]] = []
    for sym, label in NATIONAL_TEAM_20_ETFS.items():
        q = curr_quotes.get(sym, {})
        amt_yi = float(q.get("amount") or 0.0) / 1e8 if "amount" in q else float(q.get("amt_yi") or 0.0)
        if amt_yi <= 0:
            continue
        tot_amt += amt_yi
        prev_amt = float((prev_map.get(sym) or {}).get("amt_yi") or 0.0) if has_baseline else amt_yi
        damt = max(0.0, amt_yi - prev_amt) if has_baseline else 0.0
        tot_damt += damt
        movers.append((damt if has_baseline else amt_yi, amt_yi, f"`{sym[:6]}` {label}({amt_yi:.1f}亿)"))
    if tot_amt <= 0:
        return "", ""
    movers.sort(key=lambda x: -x[0])
    top_str = "、".join(m[2] for m in movers[:3])
    pulse_tag = f" [5m: +{tot_damt:.1f}亿]" if has_baseline and tot_damt > 0 else ""
    summary = f"🏦 **国家队20大重点ETF**：合计成交 **{tot_amt:.1f}亿**{pulse_tag}（常态日全天均值 563亿 / 护盘放量日 785亿+）| 领头：{top_str}"
    l2_alert = (
        f"- 🟡 **【国家队20大宽基ETF 5m放量异动】** 5分钟合计爆量 **+{tot_damt:.1f}亿**（累计 {tot_amt:.1f}亿），领头：{top_str}"
        if (has_baseline and tot_damt >= 25.0)
        else ""
    )
    return summary, l2_alert


def summarize_top_sector_mainlines(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    top_n: int = 3,
) -> str:
    if not curr_quotes or not l2_map:
        return ""
    sec_rows: dict[str, list[tuple[float, str, str, bool, bool]]] = {}
    prefix_up5: dict[str, list[str]] = {}
    for sym, q in curr_quotes.items():
        if not (sym.endswith((".SH", ".SZ", ".BJ")) and sym[:2] in ("60", "68", "00", "30", "92", "83", "87", "43")):
            continue
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        pct = ((last / pre - 1.0) * 100.0) if (last > 0 and pre > 0) else float(q.get("pct") or -999.0)
        if pct < -30.0:
            continue
        meta = l2_map.get(sym[:6], {})
        name = str(meta.get("name") or (q.get("ext") or {}).get("name") or q.get("name") or "")
        if "ST" in name:
            continue
        if pct >= 5.0 and len(name) >= 3 and name[:2] in ("东方", "中科", "华胜", "中国", "北方", "长城"):
            prefix_up5.setdefault(name[:2], []).append(f"`{sym[:6]}` {name}({pct:+.1f}%)")
        sec = str(meta.get("sector") or "")
        if sec:
            sec_rows.setdefault(sec, []).append(
                (pct, sym[:6], name, bool(meta.get("l2_eligible")), bool(meta.get("is_candidate")))
            )
    ranked: list[tuple[float, float, int, int, str, list[tuple[float, str, str, bool, bool]]]] = []
    for sec, items in sec_rows.items():
        if len(items) < 8:
            continue
        red_n = sum(1 for x in items if x[0] > 0)
        avg_pct = sum(x[0] for x in items) / len(items)
        red_ratio = red_n / len(items) * 100.0
        if avg_pct >= 0.50 and red_ratio >= 50.0:
            items.sort(key=lambda x: -x[0])
            ranked.append((avg_pct, red_ratio, red_n, len(items), sec, items))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    lines: list[str] = []
    for avg_pct, red_ratio, red_n, tot_n, sec, items in ranked[:top_n]:
        leader = f"`{items[0][1]}` {items[0][2]}({items[0][0]:+.1f}%)"
        wyckoff_picks = [f"`{c}` {n}({p:+.1f}%)" for p, c, n, is_l2, is_cand in items if (is_l2 or is_cand) and p < 9.5]
        w_tag = f" | 🎯L2可买：{'、'.join(wyckoff_picks[:2])}" if wyckoff_picks else ""
        lines.append(
            f"- **{sec}**：板块均涨 **{avg_pct:+.2f}%** | 红盘率 **{red_ratio:.0f}%** ({red_n}/{tot_n}) | 龙头：{leader}{w_tag}"
        )
    for pfx, members in sorted(prefix_up5.items(), key=lambda x: -len(x[1])):
        if len(members) >= 4:
            lines.append(f"- 🐉 **【{pfx}系字头抱团】** {len(members)} 只大涨>5%：{'、'.join(members[:4])}")
    return ("**🔥 全市场真实主线板块 TOP 3（均涨 & 红盘率）**\n" + "\n".join(lines)) if lines else ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan 7x24 financial telegraph and map to Wyckoff L2 stocks")
    parser.add_argument("--window", type=int, default=180, help="Lookback window in minutes (default 180)")
    parser.add_argument("--max-items", type=int, default=15, help="Max catalyst flashes to print")
    parser.add_argument("--pages", type=int, default=5, help="Sina 7x24 pages to scan")
    args = parser.parse_args()

    from intraday_radar import STATE_PATH, load_wyckoff_l2_map

    l2_map = load_wyckoff_l2_map(auto_fetch=False)
    state_quotes = json.loads(STATE_PATH.read_text(encoding="utf-8")).get("quotes", {}) if STATE_PATH.exists() else {}
    lines = scan_live_telegraph_catalysts(
        l2_map,
        curr_quotes=state_quotes,
        window_minutes=args.window,
        max_items=args.max_items,
        pages=args.pages,
    )
    print(f"=== 7x24 实时电报催化与威科夫映射（过去 {args.window} 分钟，共 {len(lines)} 条）===")
    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
