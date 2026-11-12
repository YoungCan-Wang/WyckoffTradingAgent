"""Real-time hot concept & constituent drill-down via TongHuaShun (10jqka) and Eastmoney."""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://eq.10jqka.com.cn/",
}
THS_HOT_PLATE_URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/plate?type=concept"
THS_BLOCK_TOP_URL = "https://data.10jqka.com.cn/dataapi/limit_up/block_top?filter=HS,GEM2STAR"
EM_CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
EM_ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"
EM_UT = "bd1d9ddb04089700cf9c27f6f7426281"

CONCEPT_ALIASES: dict[str, str] = {
    "短剧互动游戏": "短剧游戏",
    "文化传媒概念": "影视概念",
    "卫星互联网": "商业航天",
    "太阳能": "光伏概念",
    "光伏": "光伏概念",
    "CPO概念": "共封装光学(CPO)",
    "CPO": "共封装光学(CPO)",
    "数据中心": "数据中心(AIDC)",
}

NOISE_CONCEPTS = frozenset(
    {
        "融资融券",
        "深股通",
        "沪股通",
        "富时罗素",
        "标准普尔",
        "MSCI中国",
        "转债标的",
        "央国企改革",
        "国企改革",
        "专精特新",
        "创投",
        "深圳特区",
        "广东板块",
        "浙江板块",
        "江苏板块",
        "北京板块",
        "上海板块",
        "长江三角",
        "成渝特区",
        "一带一路",
        "西部大开发",
        "振兴东北",
        "华为概念",
        "百度概念",
        "腾讯概念",
        "阿里概念",
        "小米概念",
        "苹果概念",
        "特斯拉概念",
        "比亚迪概念",
        "新能源车",
        "新能源汽车",
        "人工智能",
        "数字经济",
        "芯片概念",
        "5G概念",
        "物联网",
        "大数据",
        "云计算",
        "军民融合",
        "节能环保",
        "电商概念",
        "新零售",
        "网红经济",
        "贬值受益",
        "高股息精选",
        "破净股",
        "低价股",
        "微盘股",
        "昨日涨停",
        "昨日涨停_含一字",
        "昨日连板",
        "昨日连板_含一字",
        "历史新高",
        "昨日打二板以上表现",
        "三季报增长",
        "并购重组概念",
        "股权转让",
        "增持",
    }
)


def normalize_concept_name(name: str) -> str:
    clean = name.strip()
    return CONCEPT_ALIASES.get(clean, clean)


def fetch_ths_hot_plates(timeout: float = 4.0) -> dict[str, dict[str, Any]]:
    try:
        resp = requests.get(THS_HOT_PLATE_URL, headers=THS_HEADERS, timeout=timeout)
        plates = ((resp.json().get("data") or {}).get("plate_list")) or []
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for p in plates:
        raw_name = str(p.get("name") or "").strip()
        if not raw_name:
            continue
        cname = normalize_concept_name(raw_name)
        out[cname] = {
            "code": str(p.get("code") or ""),
            "order": int(p.get("order") or 99),
            "change": float(p.get("rise_and_fall") or 0.0),
            "tag": str(p.get("tag") or ""),
            "hot_tag": str(p.get("hot_tag") or ""),
            "source": "同花顺热榜",
        }
    return out


def fetch_ths_limit_up_blocks(
    timeout: float = 4.0,
    date_ymd: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]], dict[str, str]]:
    url = f"{THS_BLOCK_TOP_URL}&date={date_ymd}" if date_ymd else THS_BLOCK_TOP_URL
    try:
        resp = requests.get(url, headers=THS_HEADERS, timeout=timeout)
        blocks = resp.json().get("data") or []
    except Exception:
        return {}, {}, {}
    block_meta: dict[str, dict[str, Any]] = {}
    code_to_blocks: dict[str, set[str]] = {}
    code_to_reason: dict[str, str] = {}
    for b in blocks:
        raw_name = str(b.get("name") or "").strip()
        if not raw_name:
            continue
        bname = normalize_concept_name(raw_name)
        if bname not in NOISE_CONCEPTS:
            block_meta[bname] = {
                "code": str(b.get("code") or ""),
                "change": float(b.get("change") or 0.0),
                "lu_num": int(b.get("limit_up_num") or 0),
                "cont_num": int(b.get("continuous_plate_num") or 0),
                "high": str(b.get("high") or ""),
                "source": "同花顺风口",
            }
        for s in b.get("stock_list") or []:
            code6 = str(s.get("code") or "").strip()
            if not code6:
                continue
            if bname not in NOISE_CONCEPTS:
                code_to_blocks.setdefault(code6, set()).add(bname)
            rtype = str(s.get("reason_type") or "").strip()
            if rtype and code6 not in code_to_reason:
                code_to_reason[code6] = rtype
            for token in rtype.split("+"):
                tok = normalize_concept_name(token)
                if tok and len(tok) >= 2 and tok not in NOISE_CONCEPTS:
                    code_to_blocks.setdefault(code6, set()).add(tok)
    return block_meta, code_to_blocks, code_to_reason


def fetch_em_hot_concepts(timeout: float = 4.0) -> dict[str, dict[str, Any]]:
    params = {
        "pn": 1,
        "pz": 25,
        "po": 1,
        "np": 1,
        "ut": EM_UT,
        "fltt": 2,
        "invt": 2,
        "fid": "f3",
        "fs": "m:90+t:3+f:!50",
        "fields": "f12,f14,f3,f104,f105,f128",
    }
    try:
        resp = requests.get(EM_CLIST_URL, params=params, timeout=timeout)
        diff = (resp.json().get("data") or {}).get("diff") or []
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in diff:
        raw_name = str(item.get("f14") or "").strip()
        if not raw_name:
            continue
        cname = normalize_concept_name(raw_name)
        if cname in NOISE_CONCEPTS:
            continue
        out[cname] = {
            "code": str(item.get("f12") or ""),
            "change": float(item.get("f3") or 0.0),
            "up": int(item.get("f104") or 0),
            "down": int(item.get("f105") or 0),
            "lead": str(item.get("f128") or ""),
            "source": "东财概念榜",
        }
    return out


def _fetch_em_ulist_chunk(secids_csv: str, timeout: float = 4.0) -> list[dict[str, Any]]:
    try:
        resp = requests.get(
            EM_ULIST_URL,
            params={"fltt": 2, "secids": secids_csv, "fields": "f12,f100,f103"},
            timeout=timeout,
        )
        return (resp.json().get("data") or {}).get("diff") or []
    except Exception:
        return []


def fetch_em_stock_concepts(symbols: Iterable[str], timeout: float = 4.0) -> dict[str, tuple[str, tuple[str, ...]]]:
    secids: list[str] = []
    for sym in symbols:
        code6 = sym[:6]
        if not code6.isdigit():
            continue
        market = "1" if sym.endswith(".SH") or code6.startswith("6") else "0"
        secids.append(f"{market}.{code6}")
    if not secids:
        return {}
    chunks = [",".join(secids[i : i + 90]) for i in range(0, len(secids), 90)]
    with ThreadPoolExecutor(max_workers=6) as ex:
        rows = [item for sub in ex.map(lambda c: _fetch_em_ulist_chunk(c, timeout), chunks) for item in sub]
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for r in rows:
        code6 = str(r.get("f12") or "").strip()
        if not code6:
            continue
        ind = str(r.get("f100") or "").strip()
        raw_concepts = str(r.get("f103") or "")
        concepts = tuple(
            dict.fromkeys(
                normalize_concept_name(c)
                for c in raw_concepts.split(",")
                if c.strip() and normalize_concept_name(c) not in NOISE_CONCEPTS
            )
        )
        out[code6] = (ind, concepts)
    return out


def _fetch_prev_ladder_blocks(snap_ymd: str | None) -> dict[str, set[str]]:
    if not snap_ymd or len(snap_ymd) != 8:
        return {}
    from datetime import datetime, timedelta

    base = datetime.strptime(snap_ymd, "%Y%m%d")
    for delta in range(1, 6):
        cand = base - timedelta(days=delta)
        if cand.weekday() >= 5:
            continue
        _, prev_blocks, _ = fetch_ths_limit_up_blocks(timeout=3.5, date_ymd=cand.strftime("%Y%m%d"))
        if prev_blocks:
            return prev_blocks
    return {}


def fetch_realtime_concept_bundle(
    target_symbols: Iterable[str],
    snap_ymd: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[str, tuple[str, ...]]], dict[str, str]]:
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_hot = ex.submit(fetch_ths_hot_plates)
        f_blk = ex.submit(fetch_ths_limit_up_blocks)
        f_prev = ex.submit(_fetch_prev_ladder_blocks, snap_ymd)
        f_em_hot = ex.submit(fetch_em_hot_concepts)
        f_em_stk = ex.submit(fetch_em_stock_concepts, list(target_symbols))

        ths_hot = f_hot.result()
        ths_blk, code_blocks, code_reasons = f_blk.result()
        prev_blocks = f_prev.result()
        em_hot = f_em_hot.result()
        em_stk = f_em_stk.result()

    for code6, bset in prev_blocks.items():
        code_blocks.setdefault(code6, set()).update(bset)

    merged_meta: dict[str, dict[str, Any]] = {}
    for cname, info in em_hot.items():
        merged_meta[cname] = dict(info)
    for cname, info in ths_blk.items():
        cur = merged_meta.setdefault(cname, {})
        cur.update({k: v for k, v in info.items() if v})
    for cname, info in ths_hot.items():
        cur = merged_meta.setdefault(cname, {})
        cur.update({k: v for k, v in info.items() if v})

    merged_stk: dict[str, tuple[str, tuple[str, ...]]] = {}
    all_codes = set(em_stk.keys()) | set(code_blocks.keys())
    for code6 in all_codes:
        ind, em_c = em_stk.get(code6, ("", ()))
        ths_c = tuple(sorted(code_blocks.get(code6, set())))
        combined = tuple(dict.fromkeys((*ths_c, *em_c)))
        merged_stk[code6] = (ind, combined)

    return merged_meta, merged_stk, code_reasons
