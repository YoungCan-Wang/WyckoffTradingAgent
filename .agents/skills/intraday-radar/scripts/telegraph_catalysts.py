#!/usr/bin/env python3
"""Real-time 7x24 financial telegraph scanner and Wyckoff L2/RPS catalyst matcher."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

CN_TZ = timezone(timedelta(hours=8))
SINA_7X24_URL = "https://zhibo.sina.com.cn/api/zhibo/feed?page={page}&page_size=40&zhibo_id=152"
WSCN_LIVE_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives?channel=global-channel&limit=25"
DEFAULT_TURNOVER_DIR = Path.home() / ".wyckoff" / "intraday_turnover"

# 来源说明：根据中央汇金/证金公司 ETF 前十大持有人披露及宽基持仓跟踪构建的 20 只核心宽基 ETF 篮子。
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
    "159845.SZ": "中证1000ETF华夏",
    "510210.SH": "上证指数ETF富国",
    "159952.SZ": "创业板ETF广发",
    "159361.SZ": "A500ETF易方达",
    "512050.SH": "A500ETF华夏",
    "159633.SZ": "中证1000ETF易方达",
    "560010.SH": "中证1000ETF广发",
    "159919.SZ": "沪深300ETF嘉实",
}

GENERIC_PREFIX_STOPWORDS = {
    "中国",
    "中信",
    "国泰",
    "中航",
    "中金",
    "中银",
    "招商",
    "华泰",
    "国信",
    "广发",
    "申万",
    "平安",
    "华润",
    "国电",
    "华能",
    "华电",
    "大唐",
    "国投",
}

CATALYST_THEMES: list[tuple[str, list[str], list[str]]] = [
    (
        "🤖 机器人/具身智能",
        ["机器人", "具身智能", "PEEK", "减速器", "滚珠丝杠", "丝杠", "灵巧手", "宇树", "智元", "外骨骼"],
        ["汽车配件", "机械基件", "机床制造", "专用机械", "电器仪表"],
    ),
    (
        "🎬 AI短剧/影视传媒",
        ["短剧", "漫剧", "影视", "院线", "Seedance", "多模态视频", "视频生成", "电影票房", "文化传媒"],
        ["影视音像", "广告包装", "出版业", "互联网", "文教休闲"],
    ),
    (
        "💻 算力/AI云/芯片",
        ["算力", "大模型", "云计算", "光模块", "芯片", "半导体", "信创", "智算中心", "液冷", "AI服务器", "晶圆"],
        ["软件服务", "IT设备", "半导体", "通信设备", "元器件"],
    ),
    (
        "⚡ 电网/特高压/绿电",
        ["电网", "特高压", "变压器", "算电协同", "电力设备", "虚拟电厂", "新型储能", "光伏组件", "风电", "煤电REIT"],
        ["电气设备", "新型电力", "火力发电", "水力发电"],
    ),
    (
        "🥇 有色/黄金/战略矿产",
        ["黄金", "白银", "铜价", "稀土", "钨", "锑", "小金属", "有色金属", "碳酸锂", "钴"],
        ["黄金", "小金属", "铜", "铝", "铅锌", "矿业"],
    ),
    (
        "🧪 化工/钢铁/周期涨价",
        ["纯碱", "磷化工", "钛白粉", "氟化工", "制冷剂", "MDI", "芳纶", "煤炭", "粗钢", "特种钢"],
        ["化工原料", "农药化肥", "化学制品", "化纤", "煤炭开采", "普钢", "特种钢"],
    ),
    (
        "🛒 消费/免税/商业零售",
        ["离岛免税", "免税店", "首发经济", "促消费", "消费券", "社会消费品", "百货零售"],
        ["百货", "超市连锁", "旅游服务", "食品", "服饰"],
    ),
    (
        "💊 医药/基因/医疗器械",
        ["创新药", "基因编辑", "脑机接口", "医疗器械", "药品集采", "GLP-1", "减肥药", "中药"],
        ["化学制药", "生物制药", "医疗保健", "中成药"],
    ),
    (
        "🌾 农业/种业/粮食安全",
        ["种业", "转基因种子", "粮食安全", "大豆", "玉米", "农林牧渔", "耕地保护", "一号文件", "农产品", "农业农村部"],
        ["种植业", "农业综合", "农药化肥", "饲料"],
    ),
    (
        "🏛️ 宏观/部委/流动性",
        [
            "央行",
            "证监会",
            "发改委",
            "商务部",
            "工信部",
            "财政部",
            "政治局",
            "国务院",
            "降准",
            "降息",
            "再贷款",
            "汇金",
        ],
        [],
    ),
]

A_SHARE_TAPE_KEYWORDS = (
    "板块",
    "概念股",
    "涨停",
    "20cm",
    "连板",
    "拉升",
    "走高",
    "走强",
    "异动",
    "A股",
    "沪指",
    "南向资金",
)

FOREIGN_ONLY_MARKERS = (
    "印度",
    "日本",
    "韩国",
    "韩财政部",
    "韩元",
    "越南",
    "印尼",
    "英国",
    "德国",
    "法国",
    "欧盟",
    "欧洲",
    "欧元区",
    "斯里兰卡",
    "土耳其",
    "巴西",
    "澳大利亚",
)
DOMESTIC_ANCHORS = ("中国", "国内", "我国", "A股", "沪深", "港股", "央行", "商务部", "工信部", "证监会", "发改委")
NEGATIVE_TAPE_MARKERS = (
    "收跌",
    "大跌",
    "暴跌",
    "跳水",
    "跌超",
    "跌停",
    "下挫",
    "走低",
    "净流出",
    "午评：",
    "午评:",
    "早评：",
    "收评：",
    "被勒令",
    "整改",
    "被立案",
    "证监会立案",
    "处罚",
    "违规",
    "减持",
    "亏损",
    "辟谣",
    "风险提示",
    "篡改",
)
STAT_SUMMARY_MARKERS = (
    "ETF两市成交",
    "ETF 两市成交",
    "两市成交额报",
    "主力资金：",
    "融资融券板块",
    "发明专利拥有量",
)


def _clean_text(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw or "").strip()
    return re.sub(r"\s+", " ", txt)


def resolve_code6_symbol(code6: str, quotes: dict[str, Any] | None = None) -> str:
    qmap = quotes or {}
    for sfx in (".SH", ".SZ", ".BJ"):
        cand = f"{code6}{sfx}"
        if cand in qmap:
            return cand
    if code6.startswith(("92", "83", "87", "88", "43")):
        return f"{code6}.BJ"
    if code6.startswith(("6", "5")):
        return f"{code6}.SH"
    return f"{code6}.SZ"


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
        except Exception as exc:
            print(f"[WARN] Sina 7x24 fetch failed on page {page}: {exc}", file=sys.stderr)
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
    except Exception as exc:
        print(f"[WARN] WSCN telegraph fetch failed: {exc}", file=sys.stderr)
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
        sym = resolve_code6_symbol(code6, quotes)
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


def _is_filtered_telegraph(text: str) -> bool:
    if any(m in text for m in NEGATIVE_TAPE_MARKERS):
        return True
    if any(f in text for f in FOREIGN_ONLY_MARKERS) and not any(d in text for d in DOMESTIC_ANCHORS):
        return True
    return False


def match_theme_and_candidates(
    text: str,
    l2_map: dict[str, dict[str, Any]],
    curr_quotes: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, list[str]]:
    if _is_filtered_telegraph(text):
        return "", []
    if any(s in text for s in STAT_SUMMARY_MARKERS):
        return "⚡ 盘面资金统计", []
    best_theme = ""
    best_sectors: list[str] = []
    best_hits = 0
    for theme_name, kws, sectors in CATALYST_THEMES:
        hits = sum(1 for k in kws if k in text)
        if hits > best_hits:
            best_hits = hits
            best_theme = theme_name
            best_sectors = sectors
    if not best_theme and not any(k in text for k in A_SHARE_TAPE_KEYWORDS):
        return "", []
    if not best_theme:
        best_theme = "⚡ 盘面异动电报"
    if not best_sectors or not l2_map:
        return best_theme, []
    return best_theme, _rank_wyckoff_picks(best_sectors, l2_map, curr_quotes or {})


def scan_live_telegraph_catalysts(
    l2_map: dict[str, dict[str, Any]],
    curr_quotes: dict[str, dict[str, Any]] | None = None,
    since_ts: float | None = None,
    window_minutes: int = 25,
    max_items: int = 5,
    pages: int = 2,
    cooldowns: dict[str, float] | None = None,
    now_ts: float | None = None,
    raw_items: list[dict[str, Any]] | None = None,
) -> list[str]:
    cur_ts = now_ts if now_ts is not None else datetime.now(CN_TZ).timestamp()
    cutoff = since_ts if (since_ts and since_ts > 0) else (cur_ts - window_minutes * 60)
    fetched = list(raw_items) if raw_items is not None else (fetch_sina_telegraph(pages=pages) + fetch_wscn_telegraph())
    fetched.sort(key=lambda x: -float(x.get("ts") or 0.0))
    seen_snippets: set[str] = set()
    lines: list[str] = []
    for item in fetched:
        if float(item.get("ts") or 0.0) <= cutoff:
            continue
        item_id = str(item.get("id") or "")
        if cooldowns is not None and item_id:
            cd_key = f"tg:{item_id}"
            if cd_key in cooldowns and (cur_ts - float(cooldowns[cd_key])) < 7200:
                continue
        txt = str(item.get("text") or "")
        dedup_key = re.sub(r"^【[^】]+】", "", txt)[:24]
        if dedup_key in seen_snippets:
            continue
        theme, picks = match_theme_and_candidates(txt, l2_map, curr_quotes)
        if not theme:
            continue
        seen_snippets.add(dedup_key)
        if cooldowns is not None and item_id:
            cooldowns[f"tg:{item_id}"] = cur_ts
        short_txt = txt[:92] + ("..." if len(txt) > 92 else "")
        pick_suffix = f" 👉 **威科夫映射**: {'、'.join(picks)}" if picks else ""
        lines.append(f"- **{item.get('hm', '')} [{theme}]** {short_txt}{pick_suffix}")
        if len(lines) >= max_items:
            break
    return lines


def _hm_to_mins(hm: str) -> int:
    parts = hm.split(":")
    return int(parts[0]) * 60 + int(parts[1]) if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() else 0


def _compare_nt_etf_same_timestamp(
    today_date: date,
    now_hm: str,
    tot_amt_yi: float,
    turnover_dir: Path | None = None,
) -> str:
    base_dir = turnover_dir or DEFAULT_TURNOVER_DIR
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        today_file = base_dir / f"nt_etf_{today_date.strftime('%Y%m%d')}.jsonl"
        today_pts: dict[str, float] = {}
        if today_file.exists():
            for ln in today_file.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    row = json.loads(ln)
                    today_pts[str(row["hm"])] = float(row["tot_amt_yi"])
        if now_hm not in today_pts:
            with open(today_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"hm": now_hm, "tot_amt_yi": round(tot_amt_yi, 2)}, ensure_ascii=False) + "\n")
        prev_files = sorted(p for p in base_dir.glob("nt_etf_*.jsonl") if p.name < today_file.name)
        if not prev_files:
            return "同时刻对比：无历史基准"
        prev_file = prev_files[-1]
        raw_ymd = prev_file.stem.replace("nt_etf_", "")
        b_date = f"{raw_ymd[:4]}-{raw_ymd[4:6]}-{raw_ymd[6:8]}" if len(raw_ymd) == 8 else raw_ymd
        prev_pts = {
            str(json.loads(ln)["hm"]): float(json.loads(ln)["tot_amt_yi"])
            for ln in prev_file.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        }
        now_min = _hm_to_mins(now_hm)
        best_hm = min(prev_pts.keys(), key=lambda k: abs(_hm_to_mins(k) - now_min), default="")
        if not best_hm or abs(_hm_to_mins(best_hm) - now_min) > 10 or prev_pts[best_hm] <= 0:
            return "同时刻对比：无历史基准"
        prev_amt = prev_pts[best_hm]
        diff_pct = (tot_amt_yi / prev_amt - 1.0) * 100.0
        return f"较 {b_date} {best_hm} {prev_amt:.1f}亿 {diff_pct:+.1f}%"
    except Exception:
        return "同时刻对比：无历史基准"


def summarize_national_team_etfs(
    curr_quotes: dict[str, dict[str, Any]],
    prev_q: dict[str, dict[str, float]] | None = None,
    has_baseline: bool = False,
    now_dt: datetime | None = None,
    turnover_dir: Path | None = None,
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
    dt = now_dt.astimezone(CN_TZ) if now_dt else datetime.now(CN_TZ)
    now_hm = dt.strftime("%H:%M")
    same_ts_cmp = _compare_nt_etf_same_timestamp(dt.date(), now_hm, tot_amt, turnover_dir=turnover_dir)
    pulse_tag = f" [5m: +{tot_damt:.1f}亿]" if has_baseline and tot_damt > 0 else ""
    summary = f"🏦 **国家队20大重点ETF**：合计成交 **{tot_amt:.1f}亿**{pulse_tag}（{same_ts_cmp}）| 领头：{top_str}"
    is_post_opening_rush = now_hm > "09:45"
    l2_alert = (
        f"- 🟡 **【国家队20大宽基ETF 5m放量异动】** 5分钟合计爆量 **+{tot_damt:.1f}亿**（累计 {tot_amt:.1f}亿），领头：{top_str}"
        if (has_baseline and is_post_opening_rush and tot_damt >= 25.0)
        else ""
    )
    return summary, l2_alert


def _detect_prefix_clusters(
    prefix_up5: dict[str, list[tuple[str, str]]],
) -> list[str]:
    out: list[str] = []
    for pfx, rows in sorted(prefix_up5.items(), key=lambda x: -len(x[1])):
        if pfx in GENERIC_PREFIX_STOPWORDS:
            continue
        sectors = {sec for _, sec in rows if sec}
        if len(rows) >= 4 and len(sectors) >= 2:
            labels = [lbl for lbl, _ in rows[:4]]
            out.append(f"- 🐉 **【{pfx}系字头抱团】** {len(rows)} 只跨板块大涨>5%：{'、'.join(labels)}")
    return out


def summarize_top_sector_mainlines(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    top_n: int = 3,
) -> str:
    if not curr_quotes or not l2_map:
        return ""
    sec_rows: dict[str, list[tuple[float, str, str, bool, bool]]] = {}
    prefix_up5: dict[str, list[tuple[str, str]]] = {}
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
        sec = str(meta.get("sector") or "")
        if pct >= 5.0 and len(name) >= 3 and re.match(r"^[\u4e00-\u9fff]{2}", name):
            prefix_up5.setdefault(name[:2], []).append((f"`{sym[:6]}` {name}({pct:+.1f}%)", sec))
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
    lines.extend(_detect_prefix_clusters(prefix_up5))
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
