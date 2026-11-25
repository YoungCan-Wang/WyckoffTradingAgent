#!/usr/bin/env python3
"""Real-time 7x24 financial telegraph scanner and Wyckoff L2/RPS catalyst matcher."""

from __future__ import annotations

import argparse
import json
import os
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

BLUECHIP_FLOOR_ETFS: frozenset[str] = frozenset(
    {"510310.SH", "510300.SH", "510050.SH", "510210.SH", "159919.SZ", "563360.SH", "159352.SZ", "159338.SZ"}
)

MAINLINE_SUPER_GROUPS: dict[str, set[str]] = {
    "大医药": {"化学制药", "中成药", "医药商业", "生物制药", "医疗保健"},
    "大消费": {
        "百货",
        "超市连锁",
        "酒店餐饮",
        "旅游景点",
        "旅游服务",
        "食品",
        "乳制品",
        "白酒",
        "啤酒",
        "红黄酒",
        "软饮料",
        "影视音像",
        "文教休闲",
        "其他商业",
        "商业连锁",
    },
    "泛AI/半导体算力": {"半导体", "元器件", "通信设备", "软件服务", "互联网", "IT设备"},
    "新能源/电力电网": {"电气设备", "火力发电", "水力发电", "新型电力", "供气供热"},
    "大周期/有色化工": {
        "化工原料",
        "石油开采",
        "石油加工",
        "煤炭开采",
        "黄金",
        "铜",
        "铝",
        "小金属",
        "铅锌",
        "普钢",
        "特种钢",
    },
    "大制造/机器人军工": {
        "专用机械",
        "工程机械",
        "机床制造",
        "机械基件",
        "汽车整车",
        "汽车配件",
        "航空",
        "船舶",
        "国防军工",
    },
    "大金融/地产": {"银行", "证券", "保险", "多元金融", "区域地产", "全国地产", "房产服务"},
}
_SUBSECTOR_TO_MAINLINE: dict[str, str] = {sub: theme for theme, subs in MAINLINE_SUPER_GROUPS.items() for sub in subs}

# 按底层赛道去重的代表性高流动性行业 ETF（同主题多只通过 THEME_ETF_KEYWORDS 归一去重）
THEME_ETF_UNIVERSE: list[tuple[str, str, list[str]]] = [
    ("物理AI/机器人", "159530.SZ", ["机器人", "具身"]),
    ("工业母机/高端制造", "159667.SZ", ["工业母机", "机床", "高端装备", "智能制造", "机械"]),
    ("通信/CPO算力", "515880.SH", ["通信", "5G", "光模块"]),
    ("半导体/芯片", "512480.SH", ["半导体", "芯片", "集成电路", "科创芯片"]),
    ("AI应用/软件", "159819.SZ", ["人工智能", "软件", "大数据", "云计算", "计算机", "传媒", "影视", "游戏"]),
    ("智能汽车/汽配", "515250.SH", ["智能汽车", "汽车", "新能车"]),
    ("消费电子/端侧", "159732.SZ", ["消费电子", "电子", "VR"]),
    ("锂电/储能", "159755.SZ", ["电池", "储能", "锂电", "新能源"]),
    ("光伏/绿电", "515790.SH", ["光伏", "电力", "绿电", "电网"]),
    ("黄金/贵金属", "518880.SH", ["黄金", "上海金", "金ETF"]),
    ("有色/战略矿产", "512400.SH", ["有色", "稀土", "矿业", "化工"]),
    ("医药/创新药", "512010.SH", ["医药", "医疗", "创新药", "生物", "中药"]),
    ("国防军工", "512660.SH", ["军工", "国防", "航空", "航天", "卫星"]),
    ("券商/多元金融", "512000.SH", ["证券", "券商", "金融科技", "保险", "银行"]),
    ("高股息红利", "515080.SH", ["红利", "高股息", "央企", "煤炭"]),
]

ETF_HEAVYWEIGHTS_MAP: dict[str, list[str]] = {
    "562500.SH": ["300124.SZ", "002230.SZ", "688169.SH", "002236.SZ", "688777.SH"],
    "159530.SZ": ["688017.SH", "002747.SZ", "002472.SZ", "300024.SZ", "603728.SH"],
    "物理AI/机器人": ["688017.SH", "002747.SZ", "002472.SZ", "300024.SZ", "603728.SH"],
    "通信/CPO算力": ["300308.SZ", "601138.SH", "300502.SZ", "300394.SZ", "000063.SZ"],
    "半导体/芯片": ["688981.SH", "688041.SH", "002371.SZ", "688256.SH", "603986.SH"],
    "AI应用/软件": ["002230.SZ", "688111.SH", "300033.SZ", "002415.SZ", "300418.SZ"],
    "智能汽车/汽配": ["002594.SZ", "601689.SH", "002920.SZ", "603596.SH", "002050.SZ"],
}

CATALYST_THEMES: list[tuple[str, list[str], list[str]]] = [
    (
        "🤖 物理AI/机器人/具身智能",
        [
            "物理AI",
            "世界模型",
            "机器人",
            "具身智能",
            "工业母机",
            "PEEK",
            "减速器",
            "滚珠丝杠",
            "丝杠",
            "灵巧手",
            "宇树",
            "智元",
            "外骨骼",
        ],
        ["汽车配件", "机械基件", "机床制造", "专用机械", "电器仪表"],
    ),
    (
        "🎬 AI短剧/影视传媒",
        ["短剧", "漫剧", "影视", "院线", "Seedance", "多模态视频", "视频生成", "电影票房", "文化传媒"],
        ["影视音像", "广告包装", "出版业", "互联网", "文教休闲"],
    ),
    (
        "💻 算力/CPO/端侧芯片",
        [
            "算力",
            "大模型",
            "云计算",
            "光模块",
            "CPO",
            "PCB",
            "高速铜缆",
            "端侧",
            "芯片",
            "半导体",
            "信创",
            "智算中心",
            "液冷",
            "AI服务器",
            "晶圆",
        ],
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
        "🚢 关税豁免/跨境出海",
        ["加征关税", "关税税则", "税委会", "暂停实施", "对美加征", "关税豁免", "出口退税", "跨境电商"],
        ["通信设备", "元器件", "轻工机械", "家用电器", "汽车配件", "电气设备"],
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
DOMESTIC_ANCHORS = (
    "中国",
    "国内",
    "我国",
    "A股",
    "沪深",
    "港股",
    "央行",
    "商务部",
    "工信部",
    "证监会",
    "发改委",
    "国务院批准",
    "税委会",
    "关税税则",
)
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
CAPEX_FCF_HEADWIND_KEYWORDS = (
    "资本开支",
    "Capex",
    "capex",
    "自由现金流",
    "FCF",
    "fcf",
    "资本支出",
)


def _has_capex_fcf_headwind(text: str, theme: str) -> bool:
    return ("算力" in theme or "芯片" in theme) and any(k in text for k in CAPEX_FCF_HEADWIND_KEYWORDS)


def _clean_text(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", "", raw or "").strip()
    return re.sub(r"\s+", " ", txt)


def _is_equity_symbol(sym: str, allow_bj: bool = False) -> bool:
    if sym.endswith(".SH"):
        return sym[:2] in ("60", "68")
    if sym.endswith(".SZ"):
        return sym[:2] in ("00", "30")
    return allow_bj and sym.endswith(".BJ") and sym[:2] in ("92", "83", "87", "88", "43")


def resolve_code6_symbol(code6: str, quotes: dict[str, Any] | None = None) -> str:
    if code6.startswith(("92", "83", "87", "88", "43")):
        return f"{code6}.BJ"
    if code6.startswith(("6", "5")):
        return f"{code6}.SH"
    if code6.startswith(("0", "3", "1")):
        return f"{code6}.SZ"
    qmap = quotes or {}
    for sfx in (".SH", ".SZ", ".BJ"):
        cand = f"{code6}{sfx}"
        if cand in qmap:
            return cand
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


def _estimate_red_ratio(quotes: dict[str, dict[str, Any]]) -> float | None:
    red = 0
    green = 0
    for sym, q in (quotes or {}).items():
        if not _is_equity_symbol(sym, allow_bj=True) or not isinstance(q, dict):
            continue
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        if last > 0 and pre > 0:
            pct = (last / pre - 1.0) * 100.0
        elif "pct" in q:
            pct = float(q.get("pct") or 0.0)
        else:
            continue
        red += int(pct > 0)
        green += int(pct < 0)
    if red + green < 50:
        return None
    return red / max(red + green, 1)


def _rank_wyckoff_picks(
    target_sectors: list[str],
    l2_map: dict[str, dict[str, Any]],
    quotes: dict[str, dict[str, Any]],
    *,
    now_hm: str = "",
    red_ratio: float | None = None,
) -> list[str]:
    scored: list[tuple[float, str]] = []
    is_afternoon_window = bool(now_hm and now_hm >= "11:00")
    max_pct = 3.8 if is_afternoon_window else 5.2
    for code6, meta in l2_map.items():
        if meta.get("sector") not in target_sectors:
            continue
        is_l2 = bool(meta.get("l2_eligible"))
        is_cand = bool(meta.get("is_candidate"))
        rps = float(meta.get("rps") or 0.0)
        chans = [str(c) for c in (meta.get("channels") or [])]
        if not is_l2 or meta.get("t1_isolated_spike"):
            continue
        if rps >= 85.0 and not is_cand and all("点火" in c for c in chans if c):
            continue
        sym = resolve_code6_symbol(code6, quotes)
        q = quotes.get(sym, {})
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        has_q = last > 0 and pre > 0
        pct = ((last - pre) / pre * 100.0) if has_q else float(q.get("pct") or 0.0)
        if pct < -1.5 or pct > max_pct:
            continue
        if has_q and last < _resolve_vwap(q, last):
            continue
        mb_bonus = 20.0 if code6.startswith(("60", "00")) else 0.0
        l2_bonus = 30.0
        rps_bonus = 15.0 if 22.0 <= rps <= 70.0 else 0.0
        streak_bonus = min(int(meta.get("streak_days") or 0), 3) * 6.0
        if is_afternoon_window:
            pct_score = pct * 2.5 if pct <= 2.8 else (7.0 - (pct - 2.8) * 8.0)
        else:
            pct_score = max(min(pct, 3.5), -1.0) * 2.0
        score = mb_bonus + l2_bonus + rps_bonus + streak_bonus + pct_score
        ch_str = "/".join(meta.get("channels") or []) or ("候选" if is_cand else f"RPS{rps:.0f}")
        raw_ftag = str(meta.get("funnel_tag") or "")
        ftag = raw_ftag.replace("L2+L3连入", "L2").replace("候选连入", "候选").replace("T-2洗盘回流", "回流")
        parts: list[str] = []
        if has_q or "pct" in q:
            parts.append(f"{pct:+.1f}%")
        if ftag and not ftag.startswith("⚠️"):
            parts.append(ftag)
        parts.append(ch_str)
        if red_ratio is not None and red_ratio < 0.45 and pct >= 2.0:
            parts.append("⚠️弱市禁追")
        scored.append((score, f"`{code6}` {meta.get('name', '')}({'·'.join(parts)})"))
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
    *,
    now_hm: str = "",
    red_ratio: float | None = None,
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
    return best_theme, _rank_wyckoff_picks(best_sectors, l2_map, curr_quotes or {}, now_hm=now_hm, red_ratio=red_ratio)


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
    *,
    now_hm: str = "",
    red_ratio: float | None = None,
) -> list[str]:
    cur_ts = now_ts if now_ts is not None else datetime.now(CN_TZ).timestamp()
    eff_hm = now_hm or datetime.fromtimestamp(cur_ts, tz=CN_TZ).strftime("%H:%M")
    eff_red_ratio = red_ratio if red_ratio is not None else _estimate_red_ratio(curr_quotes or {})
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
        theme, picks = match_theme_and_candidates(txt, l2_map, curr_quotes, now_hm=eff_hm, red_ratio=eff_red_ratio)
        if not theme:
            continue
        seen_snippets.add(dedup_key)
        if cooldowns is not None and item_id:
            cooldowns[f"tg:{item_id}"] = cur_ts
        short_txt = txt[:92] + ("..." if len(txt) > 92 else "")
        pick_suffix = f" 👉 **威科夫映射**: {'、'.join(picks)}" if picks else ""
        fcf_warn = " ⚠️[宏观FCF逆风校验：大厂Capex≠无脑利好，高开勿盲追]" if _has_capex_fcf_headwind(txt, theme) else ""
        lines.append(f"- **{item.get('hm', '')} [{theme}]** {short_txt}{pick_suffix}{fcf_warn}")
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
    eff_hm = "11:30" if "11:30" < now_hm < "12:59" else ("15:00" if now_hm > "15:00" else now_hm)
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
        if eff_hm not in today_pts:
            with open(today_file, "a", encoding="utf-8") as f:
                f.write(json.dumps({"hm": eff_hm, "tot_amt_yi": round(tot_amt_yi, 2)}, ensure_ascii=False) + "\n")
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
        now_min = _hm_to_mins(eff_hm)
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
    *,
    red_count: int | None = None,
    green_count: int | None = None,
    d_red: int | None = None,
) -> tuple[str, str]:
    prev_map = prev_q or {}
    tot_amt = 0.0
    tot_damt = 0.0
    bluechip_amt = 0.0
    growth_amt = 0.0
    movers: list[tuple[float, float, str]] = []
    for sym, label in NATIONAL_TEAM_20_ETFS.items():
        q = curr_quotes.get(sym, {})
        amt_yi = float(q.get("amount") or 0.0) / 1e8 if "amount" in q else float(q.get("amt_yi") or 0.0)
        if amt_yi <= 0:
            continue
        tot_amt += amt_yi
        if sym in BLUECHIP_FLOOR_ETFS:
            bluechip_amt += amt_yi
        else:
            growth_amt += amt_yi
        prev_amt = float((prev_map.get(sym) or {}).get("amt_yi") or 0.0) if has_baseline else amt_yi
        damt = max(0.0, amt_yi - prev_amt) if has_baseline else 0.0
        tot_damt += damt
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        pct_tag = (
            f" {(last / pre - 1.0) * 100.0:+.1f}%{'🟢' if last >= _resolve_vwap(q, last) else '🔴'}"
            if (last > 0 and pre > 0)
            else ""
        )
        movers.append((damt if has_baseline else amt_yi, amt_yi, f"`{sym[:6]}` {label}({amt_yi:.1f}亿{pct_tag})"))
    if tot_amt <= 0:
        return "", ""
    movers.sort(key=lambda x: -x[0])
    top_str = "、".join(m[2] for m in movers[:3])
    dt = now_dt.astimezone(CN_TZ) if now_dt else datetime.now(CN_TZ)
    now_hm = dt.strftime("%H:%M")
    same_ts_cmp = _compare_nt_etf_same_timestamp(dt.date(), now_hm, tot_amt, turnover_dir=turnover_dir)
    pulse_tag = f" [5m: +{tot_damt:.1f}亿]" if has_baseline and tot_damt > 0 else ""
    split_tag = f"（护盘蓝筹 {bluechip_amt:.1f}亿 / 成长弹性 {growth_amt:.1f}亿）"
    summary = f"🏦 **国家队20大重点ETF**：合计成交 **{tot_amt:.1f}亿**{pulse_tag}{split_tag}（{same_ts_cmp}）| 领头：{top_str}"
    is_post_opening_rush = now_hm > "09:45"
    has_full_breadth = red_count is not None and (red_count + (green_count or 0)) >= 100
    is_defensive_floor = has_full_breadth and (red_count < 2000 or (d_red is not None and d_red <= 0))
    if has_baseline and is_post_opening_rush and tot_damt >= 25.0:
        if is_defensive_floor:
            d_note = f"({d_red:+d})" if d_red is not None else ""
            l2_alert = (
                f"- 🛡️ **【国家队宽基托底护盘（托指不救股）】** 5分钟宽基爆量 **+{tot_damt:.1f}亿**"
                f"（累计 {tot_amt:.1f}亿，当前红盘 {red_count} 家{d_note}，属护指数非普涨信号，个股勿盲目抄底），领头：{top_str}"
            )
        else:
            l2_alert = f"- 🚀 **【国家队20大宽基ETF 5m放量异动】** 5分钟合计爆量 **+{tot_damt:.1f}亿**（累计 {tot_amt:.1f}亿），领头：{top_str}"
    else:
        l2_alert = ""
    return summary, l2_alert


def _collect_sector_metrics(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    by_sec: dict[str, list[dict[str, Any]]] = {}
    for sym, q in (curr_quotes or {}).items():
        if not _is_equity_symbol(sym, allow_bj=True):
            continue
        meta = (l2_map or {}).get(sym[:6]) or {}
        sec = str(meta.get("sector") or "")
        name = str(meta.get("name") or (q.get("ext") or {}).get("name") or q.get("name") or "")
        if not sec or "ST" in name:
            continue
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        pct = ((last / pre - 1.0) * 100.0) if (last > 0 and pre > 0) else float(q.get("pct") or -999.0)
        if pct < -30.0:
            continue
        amt_yi = float(q.get("amount") or 0.0) / 1e8 if "amount" in q else float(q.get("amt_yi") or 0.0)
        open_p = float(q.get("open") or pre or last or 0.0)
        high_p = max(float(q.get("high") or last), last) if last > 0 else 0.0
        vwap = _resolve_vwap(q, last) if last > 0 else 0.0
        by_sec.setdefault(sec, []).append(
            {
                "sym": sym,
                "code": sym[:6],
                "name": name,
                "sec": sec,
                "last": last,
                "open": open_p,
                "pct": pct,
                "amt_yi": amt_yi,
                "open_pct": ((open_p / pre - 1.0) * 100.0) if (pre > 0 and open_p > 0) else 0.0,
                "vwap_dev": ((last / vwap - 1.0) * 100.0) if (vwap > 0 and last > 0) else 0.0,
                "wick": ((high_p - last) / high_p * 100.0) if (high_p > 0 and last > 0) else 0.0,
                "is_lu": pct >= 9.5,
            }
        )
    stats: dict[str, dict[str, Any]] = {}
    for sec, items in by_sec.items():
        items.sort(key=lambda r: -r["pct"])
        tot_n = len(items)
        red_n = sum(1 for r in items if r["pct"] > 0)
        red_r = red_n / tot_n * 100.0
        avg_p = sum(r["pct"] for r in items) / tot_n
        lu_n = sum(1 for r in items if r["is_lu"])
        amt_sum = sum(r["amt_yi"] for r in items)
        conv = avg_p * 3.0 + (red_r - 50.0) * 0.08 + min(lu_n, 10) * 1.4 + min(amt_sum, 200.0) * 0.03
        stats[sec] = {
            "items": items,
            "red_n": red_n,
            "tot_n": tot_n,
            "red_ratio": red_r,
            "avg_pct": avg_p,
            "lu_count": lu_n,
            "tot_amt_yi": amt_sum,
            "conviction": conv,
        }
    return stats


def _pick_mainline_core_followers(
    items: list[dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    *,
    min_amt_yi: float = 2.0,
    top_n: int = 3,
) -> list[dict[str, Any]]:
    cands: list[tuple[tuple[int, float, float], dict[str, Any]]] = []
    for r in items:
        meta = (l2_map or {}).get(r["code"]) or {}
        rps = float(meta.get("rps") or 0.0)
        chans_s = "+".join(str(x) for x in (meta.get("channels") or []))
        is_l2 = bool(meta.get("l2_eligible")) and (("主升" in chans_s and rps <= 98.0) or rps <= 92.0)
        is_lane = meta.get("shadow_lane") in ("pre_breakout", "rotation_setup") and rps <= 95.0
        is_giant = r["amt_yi"] >= 8.0 and rps <= 95.0
        if not (is_l2 or is_lane or is_giant):
            continue
        if not (
            r["amt_yi"] >= min_amt_yi
            and r["open_pct"] <= 3.8
            and (r["open"] <= 0 or r["last"] >= r["open"])
            and 1.5 <= r["pct"] <= 7.5
            and 0.0 <= r["vwap_dev"] <= 2.2
            and r["wick"] <= 1.3
        ):
            continue
        cands.append(((1 if (is_l2 or is_lane) else 0, r["amt_yi"], -r["vwap_dev"]), r))
    cands.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in cands[:top_n]]


def detect_confirmed_mainline_alerts(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Detect slow-grinding broad A-share mainlines (e.g. Pharma/Retail) without waiting for 5m spikes."""
    stats = _collect_sector_metrics(curr_quotes, l2_map)
    grouped: dict[str, dict[str, Any]] = {}
    for sec, st in stats.items():
        if sec not in _SUBSECTOR_TO_MAINLINE and st["tot_n"] < 15:
            continue
        if st["tot_n"] < 3 or st["avg_pct"] < 1.2 or st["red_ratio"] < 70.0:
            continue
        theme = _SUBSECTOR_TO_MAINLINE.get(sec, sec)
        grp = grouped.setdefault(theme, {"sub_secs": [], "items": [], "red_n": 0, "tot_n": 0, "lu_n": 0, "amt_yi": 0.0})
        grp["sub_secs"].append((st["lu_count"], st["tot_amt_yi"], sec))
        grp["items"].extend(st["items"])
        grp["red_n"] += st["red_n"]
        grp["tot_n"] += st["tot_n"]
        grp["lu_n"] += st["lu_count"]
        grp["amt_yi"] += st["tot_amt_yi"]
    alerts: list[tuple[tuple[int, float], str, str, dict[str, Any]]] = []
    for theme, grp in grouped.items():
        tot_n = grp["tot_n"]
        if tot_n < 15:
            continue
        red_r = grp["red_n"] / tot_n * 100.0
        avg_p = sum(r["pct"] for r in grp["items"]) / tot_n
        if not (red_r >= 78.0 and avg_p >= 1.8 and grp["lu_n"] >= 3 and grp["amt_yi"] >= 25.0):
            continue
        grp["items"].sort(key=lambda r: -r["pct"])
        grp["sub_secs"].sort(reverse=True)
        sub_lbl = "+".join(s[2] for s in grp["sub_secs"][:3])
        leaders = "、".join(f"`{r['code']}` {r['name']}({r['pct']:+.1f}%)" for r in grp["items"][:2])
        cores = _pick_mainline_core_followers(grp["items"], l2_map, min_amt_yi=2.0, top_n=3)
        f_tag = (
            " 👉 🎯中军可跟："
            + "、".join(f"`{c['code']}` {c['name']}({c['pct']:+.1f}%·均+{c['vwap_dev']:.1f}%)" for c in cores)
            if cores
            else ""
        )
        md = (
            f"- 🔥 **【主线成型·中军战法】{theme}（{sub_lbl}）**：红盘 **{red_r:.0f}%** ({grp['red_n']}/{tot_n}) | "
            f"均涨 **{avg_p:+.2f}%** | **{grp['lu_n']}只涨停** | 额 {grp['amt_yi']:.0f}亿 | 标杆：{leaders}{f_tag}"
        )
        rec = {
            "theme": theme,
            "red_ratio": round(red_r, 1),
            "avg_pct": round(avg_p, 2),
            "lu_count": grp["lu_n"],
            "tot_amt_yi": round(grp["amt_yi"], 1),
        }
        alerts.append(((grp["lu_n"], grp["amt_yi"]), theme, md, rec))
    alerts.sort(key=lambda x: x[0], reverse=True)
    return [(theme, md, rec) for _, theme, md, rec in alerts]


def summarize_top_sector_mainlines(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    top_n: int = 5,
) -> str:
    if not curr_quotes or not l2_map:
        return ""
    stats = _collect_sector_metrics(curr_quotes, l2_map)
    ranked = sorted(
        (
            (st, sec)
            for sec, st in stats.items()
            if st["tot_n"] >= 8 and st["avg_pct"] >= 0.50 and st["red_ratio"] >= 50.0
        ),
        key=lambda kv: (kv[0]["conviction"], kv[0]["avg_pct"], kv[0]["red_ratio"]),
        reverse=True,
    )
    lines: list[str] = []
    for st, sec in ranked[:top_n]:
        top = st["items"][0]
        leader = f"`{top['code']}` {top['name']}({top['pct']:+.1f}%)"
        cores = _pick_mainline_core_followers(st["items"], l2_map, min_amt_yi=2.0, top_n=2)
        picks = [f"`{c['code']}` {c['name']}({c['pct']:+.1f}%)" for c in cores]
        if not picks and st["tot_amt_yi"] <= 0:
            picks = [
                f"`{r['code']}` {r['name']}({r['pct']:+.1f}%)"
                for r in st["items"]
                if (
                    (l2_map.get(r["code"]) or {}).get("l2_eligible")
                    or (l2_map.get(r["code"]) or {}).get("shadow_lane") == "pre_breakout"
                )
                and float((l2_map.get(r["code"]) or {}).get("rps") or 0.0) <= 88.0
                and 0.5 <= r["pct"] <= 6.8
            ]
        lu_part = f" | 🔥{st['lu_count']}涨停" if st["lu_count"] > 0 else ""
        amt_part = f" | 额 {st['tot_amt_yi']:.0f}亿" if st["tot_amt_yi"] > 0 else ""
        w_tag = f" | 🎯中军可跟：{'、'.join(picks[:2])}" if picks else ""
        lines.append(
            f"- **{sec}**：板块均涨 **{st['avg_pct']:+.2f}%** | 红盘率 **{st['red_ratio']:.0f}%** "
            f"({st['red_n']}/{st['tot_n']}){lu_part}{amt_part} | 龙头：{leader}{w_tag}"
        )
    return (
        (f"**🔥 全市场真实主线板块 TOP {top_n}（强度/涨停密度/成交额综合排行）**\n" + "\n".join(lines)) if lines else ""
    )


def _classify_etf_theme_bucket(sym: str, name: str) -> str:
    if sym[:3] in ("511", "513"):
        return ""
    non_a_markers = (
        "纳指",
        "纳斯达克",
        "标普",
        "道琼斯",
        "美国",
        "日经",
        "日本",
        "东证",
        "恒生",
        "港股",
        "H股",
        "中概",
        "德国",
        "法国",
        "沙特",
        "亚太",
        "东南亚",
        "韩国",
        "海外",
        "全球",
        "跨境",
        "QDII",
        "国债",
        "政金债",
        "可转债",
        "公司债",
        "城投债",
        "短融",
        "货币",
        "添益",
        "日利",
    )
    broad_markers = (
        "沪深300",
        "上证50",
        "上证180",
        "中证500",
        "中证800",
        "中证1000",
        "中证2000",
        "A500",
        "A50",
        "创业板",
        "科创50",
        "科创100",
        "科创200",
        "科创创业",
        "双创",
        "深证100",
        "主动ETF",
    )
    if sym in NATIONAL_TEAM_20_ETFS or any(w in name for w in (*broad_markers, *non_a_markers)):
        return ""
    for bucket, anchor_sym, kws in THEME_ETF_UNIVERSE:
        if sym == anchor_sym or any(k in name for k in kws):
            return bucket
    stem = name.split("ETF")[0].strip() if "ETF" in name else name[:6]
    return stem or sym


def check_etf_heavyweight_divergence(
    sym: str,
    theme: str,
    curr_quotes: dict[str, dict[str, Any]],
) -> str:
    weights = ETF_HEAVYWEIGHTS_MAP.get(sym) or ETF_HEAVYWEIGHTS_MAP.get(theme) or []
    if not weights:
        return ""
    pcts: list[float] = []
    for w in weights:
        q = curr_quotes.get(w)
        if not q:
            continue
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        if last > 0 and pre > 0:
            pcts.append((last / pre - 1.0) * 100.0)
    if len(pcts) < 3:
        return ""
    up_n = sum(1 for p in pcts if p > 0)
    avg_p = sum(pcts) / len(pcts)
    if up_n / len(pcts) < 0.60 or avg_p < 0.30:
        return f"⚠️重仓股背离({up_n}/{len(pcts)}红·均{avg_p:+.1f}%，小票涨停ETF不跟)"
    return ""


def rank_deduplicated_theme_etfs(
    curr_quotes: dict[str, dict[str, Any]],
    *,
    min_amt_yi: float = 0.5,
    top_n: int = 4,
) -> list[dict[str, Any]]:
    anchor_syms = {a for _, a, _ in THEME_ETF_UNIVERSE}
    best_by_theme: dict[str, dict[str, Any]] = {}
    for sym, q in (curr_quotes or {}).items():
        if not ((sym.endswith(".SH") and sym[:2] in ("51", "56", "58")) or (sym.endswith(".SZ") and sym[:2] == "15")):
            continue
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        amt_yi = float(q.get("amount") or 0.0) / 1e8 if "amount" in q else float(q.get("amt_yi") or 0.0)
        if last <= 0 or pre <= 0 or amt_yi < min_amt_yi:
            continue
        name = str((q.get("ext") or {}).get("name") or q.get("name") or sym)
        bucket = _classify_etf_theme_bucket(sym, name)
        if not bucket:
            continue
        vwap = _resolve_vwap(q, last)
        pct = (last / pre - 1.0) * 100.0
        v_dev = (last / vwap - 1.0) * 100.0 if vwap > 0 else 0.0
        cand = {
            "theme": bucket,
            "symbol": sym,
            "name": name,
            "last": last,
            "pct": pct,
            "vwap_dev": v_dev,
            "amt_yi": amt_yi,
        }
        prev = best_by_theme.get(bucket)
        cand_anchor = sym in anchor_syms and amt_yi >= 1.0
        prev_anchor = prev is not None and prev["symbol"] in anchor_syms and prev["amt_yi"] >= 1.0
        take = (
            prev is None
            or (cand_anchor and not prev_anchor and pct >= prev["pct"] - 0.5)
            or (
                not prev_anchor
                and ((amt_yi > prev["amt_yi"] * 1.25) or (amt_yi >= prev["amt_yi"] * 0.6 and pct > prev["pct"]))
            )
            or (prev_anchor and amt_yi >= prev["amt_yi"] * 1.5 and pct > prev["pct"] + 0.5)
        )
        if take:
            best_by_theme[bucket] = cand
    ranked = [v for v in best_by_theme.values() if v["pct"] > 0.3 and v["vwap_dev"] >= -0.2]
    ranked.sort(key=lambda r: (r["pct"], r["amt_yi"]), reverse=True)
    return ranked[:top_n]


def _sina_kline_symbol(sym: str) -> str:
    code, _, ex = sym.partition(".")
    return f"{ex.lower()}{code}" if ex in ("SH", "SZ") and len(code) == 6 else ""


def fetch_daily_chip_bars(
    symbols: list[str],
    today_str: str,
    cache_dir: Path | None = None,
) -> dict[str, list[dict[str, float]]]:
    cache_file = (cache_dir or Path("/tmp")) / f"wyckoff_chip_bars_{today_str}.json"
    cached: dict[str, list[dict[str, float]]] = {}
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached = {}
    if cache_dir is None and os.environ.get("PYTEST_CURRENT_TEST"):
        return {s: cached[s] for s in symbols if s in cached}
    updated = False
    for sym in dict.fromkeys(symbols):
        if sym in cached or not (sc := _sina_kline_symbol(sym)):
            continue
        url = (
            f"https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_{sc}=/"
            f"CN_MarketDataService.getKLineData?symbol={sc}&scale=240&ma=no&datalen=8"
        )
        try:
            req = urllib.request.Request(url, headers={"Referer": "https://finance.sina.com.cn"})
            raw = urllib.request.urlopen(req, timeout=3.0).read().decode("utf-8", errors="ignore")
            arr = json.loads(raw[raw.find("[") : raw.rfind("]") + 1])
            bars = [
                {
                    "day": str(b["day"]),
                    "open": float(b["open"]),
                    "high": float(b["high"]),
                    "low": float(b["low"]),
                    "close": float(b["close"]),
                    "volume": float(b["volume"]),
                }
                for b in arr
                if str(b.get("day", "")) < today_str and float(b.get("close") or 0) > 0
            ][-5:]
            if bars:
                cached[sym] = bars
                updated = True
        except Exception:
            continue
    if updated:
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return {s: cached[s] for s in symbols if s in cached}


def _index_open_and_pct(curr_quotes: dict[str, dict[str, Any]], sym: str) -> tuple[float, float]:
    q = curr_quotes.get(sym) or {}
    last = float(q.get("last_price") or q.get("price") or 0.0)
    pre = float(q.get("prev_close") or 0.0)
    opn = float(q.get("open") or 0.0)
    open_pct = (opn / pre - 1.0) * 100.0 if opn > 0 and pre > 0 else 0.0
    pct = (last / pre - 1.0) * 100.0 if last > 0 and pre > 0 else 0.0
    return open_pct, pct


def evaluate_t1_chip_regime(
    curr_quotes: dict[str, dict[str, Any]],
    daily_bars: dict[str, list[dict[str, Any]]] | None = None,
    red_ratio: float | None = None,
) -> tuple[str, str]:
    cy_open, cy_pct = _index_open_and_pct(curr_quotes, "399006.SZ")
    sh_open, sh_pct = _index_open_and_pct(curr_quotes, "000001.SH")
    bars = (daily_bars or {}).get("399006.SZ") or (daily_bars or {}).get("000001.SH") or []
    is_cy = bool((daily_bars or {}).get("399006.SZ"))
    if bars:
        t1 = bars[-1]
        t1_bounce = (t1["close"] / max(t1["low"], 1e-6) - 1.0) * 100.0
        nt_bars = (daily_bars or {}).get("510300.SH") or []
        nt_prior_avg = (sum(b["volume"] for b in nt_bars[:-1]) / len(nt_bars[:-1])) if len(nt_bars) >= 2 else 0.0
        nt_vol_ratio = (nt_bars[-1]["volume"] / nt_prior_avg) if nt_prior_avg > 0 else 1.0
        if t1_bounce >= (3.0 if is_cy else 1.5) and (nt_vol_ratio < 0.85 or cy_open >= 1.0 or sh_open >= 0.5):
            nt_note = f"、510300量比 {nt_vol_ratio:.2f}x 无国家队锁仓" if nt_vol_ratio < 0.85 else ""
            return (
                "CHIP_DUMP_LOCK",
                f"💣 **【T+1 筹码兑现禁买令】昨日指数低点反弹 +{t1_bounce:.1f}%{nt_note}，今早开盘 {cy_open if is_cy else sh_open:+.2f}%：14:35 前严禁开新仓接获利盘飞刀（只卖不买），唯一新仓评估锁死 14:40 换手洗透后！**",
            )
        if len(bars) >= 3:
            prev_c, peak_h = bars[-2]["close"], max(b["high"] for b in bars[:-1])
            drawdown = (t1["close"] / max(peak_h, 1e-6) - 1.0) * 100.0
            open_ref, pct_ref = (cy_open, cy_pct) if is_cy and cy_pct != 0.0 else (sh_open, sh_pct)
            if (
                t1["close"] <= prev_c * 1.002
                and t1_bounce <= 1.2
                and drawdown <= -2.5
                and open_ref <= 1.0
                and ((red_ratio or 0.0) >= 0.50 or (pct_ref - open_ref) >= 0.8)
            ):
                return (
                    "WASHOUT_FIRST_DAY",
                    f"🌱 **【恐慌出清首日·开放早盘先手窗】前波回撤 {drawdown:.1f}% 且昨日收于低位（T+1 短线获利盘≈0），09:45~10:35 放行低偏离且远离套牢峰的主线 ETF 右侧先手（不必死等尾盘抬轿）！**",
                )
    if (cy_open >= 1.8 and cy_pct - cy_open <= -0.6) or (sh_open >= 0.8 and sh_pct - sh_open <= -0.5):
        return (
            "CHIP_DUMP_LOCK",
            f"💣 **【高开派发筹码兑现禁买令】指数高开回落（上证开 {sh_open:+.2f}%→现 {sh_pct:+.2f}%）：14:35 前严禁追高开新仓，等 14:40 换手出清！**",
        )
    return "NEUTRAL", ""


def check_symbol_chip_overhang(last: float, bars: list[dict[str, Any]] | None) -> str:
    if not bars or last <= 0:
        return ""
    t1, prior = bars[-1], bars[:-1]
    gain_24h = (last / max(t1["low"], 1e-6) - 1.0) * 100.0
    avg_vol = (sum(b["volume"] for b in prior) / len(prior)) if prior else 0.0
    vol_ratio = (t1["volume"] / avg_vol) if avg_vol > 0 else 1.0
    overhead = max((b["high"] for b in prior), default=0.0)
    hit_overhead = overhead > 0 and t1["close"] < overhead * 0.992 and overhead * 0.988 <= last <= overhead * 1.03
    if gain_24h >= 6.5 and vol_ratio >= 1.25:
        return f"💣T+1获利盘+{gain_24h:.1f}%{'+撞前高' + format(overhead, '.3f') if hit_overhead else ''}"
    return f"💣撞前高套牢峰{overhead:.3f}" if hit_overhead else ""


def build_morning_washout_card(
    curr_quotes: dict[str, dict[str, Any]],
    daily_bars: dict[str, list[dict[str, Any]]] | None,
    red_ratio: float | None,
) -> str:
    if (red_ratio or 0.0) < 0.48:
        return ""
    leaders = rank_deduplicated_theme_etfs(curr_quotes, min_amt_yi=0.8, top_n=4)
    top1_pct = leaders[0]["pct"] if leaders else 0.0
    picked: list[str] = []
    for item in leaders:
        last, bars = item["last"], (daily_bars or {}).get(item["symbol"])
        if not (0.8 <= item["pct"] <= 4.0 and 0.0 <= item["vwap_dev"] <= 1.6):
            continue
        if top1_pct >= 4.5 and (red_ratio or 0.0) < 0.68 and item["pct"] < top1_pct * 0.50:
            continue
        if check_symbol_chip_overhang(last, bars) or check_etf_heavyweight_divergence(
            item["symbol"], item["theme"], curr_quotes
        ):
            continue
        if bars and len(bars) >= 2 and last > max(b["high"] for b in bars[:-1]) * 0.98:
            continue
        picked.append(
            f"- **[{item['theme']}] `{item['symbol']}` {item['name']}**："
            f"现价 **{last:.3f} ({item['pct']:+.2f}%)** | VWAP乖离 **{item['vwap_dev']:+.2f}%** | "
            f"额 {item['amt_yi']:.1f}亿 | 🛑-3%硬止损 **{last * 0.97:.3f}** | 🛡️+4%保本 **{last * 1.04:.3f}**"
        )
    return (
        ("**🌱 【早盘冰点出清先手卡：T+1零获利盘真空区·右侧低偏离上车】**\n" + "\n".join(picked[:2])) if picked else ""
    )


def summarize_theme_etf_leadership(
    curr_quotes: dict[str, dict[str, Any]],
    *,
    red_ratio: float | None = None,
    now_hm: str = "",
    chip_regime: str = "",
    daily_bars: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    leaders = rank_deduplicated_theme_etfs(curr_quotes, min_amt_yi=0.5, top_n=4)
    if not leaders:
        return ""
    lines: list[str] = []
    for item in leaders:
        vd, pct = item["vwap_dev"], item["pct"]
        chip_warn = check_symbol_chip_overhang(item["last"], (daily_bars or {}).get(item["symbol"]))
        hw_warn = check_etf_heavyweight_divergence(item["symbol"], item["theme"], curr_quotes)
        if vd > 2.5 or pct > 4.5:
            zone = "⛔偏离过高禁追·等次日回踩"
        elif (not now_hm or now_hm < "14:35") and (chip_regime == "CHIP_DUMP_LOCK" or chip_warn):
            zone = f"{chip_warn or '💣T+1筹码兑现锁死'}·禁早盘追买(等14:40)"
        elif hw_warn:
            zone = f"{hw_warn}·禁买错配ETF"
        elif (
            chip_regime == "WASHOUT_FIRST_DAY"
            and "09:45" <= now_hm <= "10:35"
            and (red_ratio is None or red_ratio >= 0.48)
            and 0.8 <= pct <= 3.8
            and 0.0 <= vd <= 1.6
        ):
            zone = f"🌱冰点出清早盘先手·🎯黄金低偏离起涨窗(止损{item['last'] * 0.97:.3f})"
        elif chip_regime == "NEUTRAL" and now_hm and now_hm < "14:35" and 1.0 <= pct <= 3.5 and 0.0 <= vd <= 1.6:
            zone = "👀常态盘中观察·等14:40尾盘明牌"
        elif (red_ratio is None or red_ratio >= 0.38) and now_hm >= "09:50" and 1.0 <= pct <= 3.5 and 0.0 <= vd <= 1.6:
            zone = "🎯黄金低偏离起涨窗"
        else:
            zone = "👀均线上观察"
        lines.append(
            f"- **[{item['theme']}] `{item['symbol']}` {item['name']}**：现价 **{item['last']:.3f} ({pct:+.2f}%)** | "
            f"VWAP乖离 **{vd:+.2f}%** | 额 {item['amt_yi']:.2f}亿 | {zone}"
        )
    return "**🏆 去重主题 ETF 主航道（同赛道仅留流动性龙头）**\n" + "\n".join(lines)


def _resolve_vwap(q: dict[str, Any], last: float) -> float:
    if "vwap" in q and float(q.get("vwap") or 0.0) > 0:
        return float(q["vwap"])
    vol = float(q.get("volume") or 0.0)
    amt = float(q.get("amount") or 0.0)
    raw = amt / vol if vol > 0 else last
    if 0.5 < (raw / last) < 1.5:
        return raw
    if 0.5 < (raw / 100.0 / last) < 1.5:
        return raw / 100.0
    return (float(q.get("high") or last) + float(q.get("low") or last) + last) / 3.0


def _collect_strong_sectors(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
) -> dict[str, tuple[float, float]]:
    sec_pcts: dict[str, list[float]] = {}
    l2_up_counts: dict[str, int] = {}
    for sym, q in curr_quotes.items():
        if not _is_equity_symbol(sym):
            continue
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        meta = l2_map.get(sym[:6]) or {}
        sec = str(meta.get("sector") or "")
        if last > 0 and pre > 0 and sec:
            pct = (last / pre - 1.0) * 100.0
            sec_pcts.setdefault(sec, []).append(pct)
            if pct >= 2.0 and (meta.get("l2_eligible") or meta.get("is_candidate")):
                l2_up_counts[sec] = l2_up_counts.get(sec, 0) + 1
    return {
        s: (sum(1 for p in ps if p > 0) / len(ps) * 100.0, sum(ps) / len(ps))
        for s, ps in sec_pcts.items()
        if len(ps) >= 8 and ((sum(1 for p in ps if p > 0) / len(ps)) >= 0.75 or l2_up_counts.get(s, 0) >= 4)
    }


def rank_intraday_rs_leaders(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    top_n: int = 3,
) -> list[dict[str, Any]]:
    """Select intraday relative-strength cores from empirically positive-alpha pools."""
    sec_stats = _collect_sector_metrics(curr_quotes, l2_map)
    hot_secs = {
        sec
        for _, sec in sorted(
            (
                (st["conviction"], sec)
                for sec, st in sec_stats.items()
                if st["tot_n"] >= 8 and st["avg_pct"] >= 1.5 and st["red_ratio"] >= 75.0
            ),
            reverse=True,
        )[:6]
    }
    for theme, _, _ in detect_confirmed_mainline_alerts(curr_quotes, l2_map):
        hot_secs.update(MAINLINE_SUPER_GROUPS.get(theme, {theme}))
    scored: list[tuple[float, str, dict[str, Any]]] = []
    for sym, q in (curr_quotes or {}).items():
        if not _is_equity_symbol(sym):
            continue
        meta = (l2_map or {}).get(sym[:6]) or {}
        name = str(meta.get("name") or (q.get("ext") or {}).get("name") or q.get("name") or "")
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        if "ST" in name or last <= 0 or pre <= 0:
            continue
        amt_yi = float(q.get("amount") or 0.0) / 1e8 if "amount" in q else float(q.get("amt_yi") or 0.0)
        if amt_yi < 3.0:
            continue
        open_p = float(q.get("open") or pre)
        open_pct = (open_p / pre - 1.0) * 100.0
        pct = (last / pre - 1.0) * 100.0
        high_p = max(float(q.get("high") or last), last)
        upper_wick_pct = (high_p - last) / high_p * 100.0 if high_p > 0 else 0.0
        vwap = _resolve_vwap(q, last)
        vwap_dev = (last / vwap - 1.0) * 100.0 if vwap > 0 else 0.0
        if not (
            open_pct <= 3.8
            and last >= open_p
            and 1.8 <= pct <= 7.5
            and 0.0 <= vwap_dev <= 2.2
            and upper_wick_pct <= 1.2
        ):
            continue
        sec = str(meta.get("sector") or "")
        rps = float(meta.get("rps") or 0.0)
        chans_s = "+".join(str(x).strip() for x in (meta.get("channels") or []))
        is_main = bool(
            (meta.get("l2_eligible") and "主升" in chans_s and 55.0 <= rps <= 88.0)
            or (meta.get("shadow_lane") == "pre_breakout" and 50.0 <= rps <= 88.0)
        )
        is_hot_core = bool(
            sec in hot_secs
            and (meta.get("l2_eligible") or meta.get("shadow_lane") in ("pre_breakout", "rotation_setup"))
            and rps <= 98.0
        )
        is_giant = amt_yi >= 20.0 and 20.0 <= rps <= 82.0 and open_pct <= 3.0
        is_accum = bool(
            meta.get("l2_eligible")
            and meta.get("l3_eligible")
            and ("地量" in chans_s or "吸筹" in chans_s)
            and 24.0 <= rps <= 80.0
            and (int(meta.get("streak_days") or 0) >= 2 or meta.get("stage") != "风控拦截")
        )
        tag, bonus = (
            ("🏆主升/前夜", 32.0 if sec in hot_secs else 30.0)
            if is_main
            else (
                ("🔥主线中军", 32.0)
                if is_hot_core
                else (("🏛️百亿抗跌中军", 26.0) if is_giant else (("🛡️多日蓄势RS", 16.0) if is_accum else ("", 0.0)))
            )
        )
        if not tag:
            continue
        score = bonus + min(amt_yi, 30.0) * 1.2 + pct * 2.0 - vwap_dev * 3.0
        scored.append(
            (
                score,
                sec,
                {
                    "symbol": sym,
                    "name": name,
                    "sector": sec,
                    "last": last,
                    "pct": pct,
                    "vwap": vwap,
                    "vwap_dev": vwap_dev,
                    "upper_wick_pct": upper_wick_pct,
                    "amt_yi": amt_yi,
                    "tag": tag,
                    "rps": rps,
                },
            )
        )
    scored.sort(key=lambda x: -x[0])
    picked, sec_counts = [], {}
    for _, sec, row in scored:
        if sec and sec_counts.get(sec, 0) >= 2:
            continue
        picked.append(row)
        if sec:
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
        if len(picked) >= top_n:
            break
    return picked


def summarize_zero_bias_l2_leaders(
    curr_quotes: dict[str, dict[str, Any]],
    l2_map: dict[str, dict[str, Any]],
    top_n: int = 4,
) -> str:
    if not curr_quotes or not l2_map:
        return ""
    strong_secs = _collect_strong_sectors(curr_quotes, l2_map)
    scored: list[tuple[float, str, str, str]] = []
    for sym, q in curr_quotes.items():
        if not _is_equity_symbol(sym):
            continue
        meta = l2_map.get(sym[:6]) or {}
        sec = str(meta.get("sector") or "")
        rps = float(meta.get("rps") or 0.0)
        raw_ch = meta.get("channels") or []
        chans = [c.strip() for x in raw_ch for c in str(x).split("+") if c.strip()]
        chans_s = "+".join(chans)
        has_steady_ch = any("地量" in c or "吸筹" in c for c in chans)
        is_main_or_pre = ("主升" in chans_s and 55.0 <= rps <= 88.0) or (
            meta.get("shadow_lane") == "pre_breakout" and 50.0 <= rps <= 88.0
        )
        if not (
            (meta.get("l2_eligible") or meta.get("shadow_lane") == "pre_breakout")
            and (has_steady_ch or is_main_or_pre)
            and sec in strong_secs
        ):
            continue
        if has_steady_ch and not is_main_or_pre and (not meta.get("l3_eligible") or not (12.0 <= rps <= 80.0)):
            continue
        name = str(meta.get("name") or (q.get("ext") or {}).get("name") or q.get("name") or "")
        last = float(q.get("last_price") or q.get("price") or 0.0)
        pre = float(q.get("prev_close") or 0.0)
        if "ST" in name or last <= 0 or pre <= 0:
            continue
        pct = (last / pre - 1.0) * 100.0
        amt_yi = float(q.get("amount") or 0.0) / 1e8 if "amount" in q else float(q.get("amt_yi") or 0.0)
        vwap = _resolve_vwap(q, last)
        open_p = float(q.get("open") or pre)
        high_p = max(float(q.get("high") or last), last)
        low_p = min(float(q.get("low") or last), last) if float(q.get("low") or 0.0) > 0 else last
        if not (1.5 <= pct <= 6.8 and amt_yi >= 1.5 and vwap * 1.001 <= last <= vwap * 1.022):
            continue
        if last < open_p or (high_p - last) / high_p > 0.012 or (high_p - low_p) / pre > 0.065:
            continue
        red_r, avg_p = strong_secs[sec]
        streak_n = int(meta.get("streak_days") or 0)
        if red_r >= 75.0:
            if pct < avg_p * 0.75:
                continue
        elif streak_n < 2 or pct < max(avg_p + 2.5, 2.0):
            continue
        rps_sweet = 18.0 if (is_main_or_pre or 24.0 <= rps <= 68.0) else 0.0
        streak_bonus = min(streak_n, 3) * 6.0
        score = (
            (20.0 if (meta.get("is_candidate") or is_main_or_pre) else 0.0)
            + rps_sweet
            + streak_bonus
            + len(chans) * 8.0
            + (pct + avg_p) * 3.0
            + min(amt_yi, 10.0) * 2.0
        )
        v_dev = (last / vwap - 1.0) * 100.0
        f_tag = f" [{meta['funnel_tag']}]" if meta.get("funnel_tag") else ""
        ch_lbl = chans_s or ("前夜pre_breakout" if meta.get("shadow_lane") == "pre_breakout" else "")
        scored.append(
            (
                score,
                sec,
                sym,
                f"- **{sym} {name}** [{sec} 红{red_r:.0f}%]：现价 **{last:.2f} ({pct:+.2f}%)** | "
                f"均价 {vwap:.2f} (乖离 {v_dev:+.2f}%) | 额 {amt_yi:.1f}亿 | {ch_lbl}{f_tag} RPS={rps:.0f}",
            )
        )
    scored.sort(key=lambda x: -x[0])
    picked, seen_syms, sec_counts = [], set(), {}
    for _, sec, sym, line in scored:
        if sec_counts.get(sec, 0) >= 2:
            continue
        picked.append(line)
        seen_syms.add(sym)
        sec_counts[sec] = sec_counts.get(sec, 0) + 1
        if len(picked) >= top_n:
            break
    for r in rank_intraday_rs_leaders(curr_quotes, l2_map, top_n=top_n):
        if len(picked) >= top_n or r["symbol"] in seen_syms:
            continue
        sec = r["sector"]
        if sec and sec_counts.get(sec, 0) >= 2:
            continue
        picked.append(
            f"- **[{r['tag']}] {r['symbol']} {r['name']}** [{sec}]：现价 **{r['last']:.2f} ({r['pct']:+.2f}%)** | "
            f"VWAP乖离 **{r['vwap_dev']:+.2f}%** | 额 {r['amt_yi']:.1f}亿 | RPS={r['rps']:.0f}"
        )
        seen_syms.add(r["symbol"])
        if sec:
            sec_counts[sec] = sec_counts.get(sec, 0) + 1
    return ("**👀 全市场 L2+L3 稳健中军盘中观察池**\n" + "\n".join(picked)) if picked else ""


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
