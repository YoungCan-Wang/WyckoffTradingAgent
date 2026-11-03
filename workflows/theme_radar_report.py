"""Markdown and HTML rendering for strategic theme radar snapshots."""

from __future__ import annotations

from html import escape

STATE_LABELS = {
    "observe": "萌芽观察",
    "confirmed": "主线确认",
    "extension": "趋势延续",
    "overheated": "过热拥挤",
    "decay": "噪音/衰退",
}
ROTATION_STATE_LABELS = {"surging": "加速", "rising": "升温", "quiet": "平静"}


def render_theme_radar_report(snapshot: dict) -> str:
    themes = snapshot.get("themes", []) or []
    rotation = snapshot.get("rotation_watch", []) or []
    clusters = snapshot.get("cluster_watch", []) or []
    grouped = _candidates_by_theme(snapshot.get("strategic_candidates", []) or [])
    lines = [
        "# Theme Radar",
        "",
        f"**交易日**: {snapshot.get('trade_date', '')}",
        "",
        "## 🚦 一眼结论",
        "",
        _radar_verdict(themes, rotation),
        "",
        "> 短周期轮动只做 Shadow 发现，不代表允许买入；正式操作仍需长期主题、买点、市场总闸和 OMS 共同通过。",
        "",
        "## 🔥 短周期轮动（Shadow）",
        "",
    ]
    lines.extend(_rotation_table(rotation))
    lines.extend(["", "## 🤝 抱团共振（Shadow）", ""])
    lines.extend(_cluster_table(clusters))
    lines.extend(["", "## 🧭 中长线主线", ""])
    lines.extend(_theme_table(themes))
    lines.extend(["", "## 按主题展开", ""])
    lines.extend(_theme_sections(themes, grouped))
    if not themes and grouped:
        lines.extend(_orphan_candidate_sections(grouped))
    return "\n".join(lines)


def render_theme_radar_html(snapshot: dict) -> str:
    themes = snapshot.get("themes", []) or []
    rotation = snapshot.get("rotation_watch", []) or []
    grouped = _candidates_by_theme(snapshot.get("strategic_candidates", []) or [])
    theme_cards = "\n".join(_theme_card_html(item, grouped.get(str(item.get("theme", "")), [])) for item in themes)
    orphan_cards = "\n".join(
        _orphan_card_html(theme, rows) for theme, rows in grouped.items() if theme not in _theme_names(themes)
    )
    return _html_shell(snapshot, _rotation_board_html(themes, rotation) + theme_cards + orphan_cards)


def _radar_verdict(themes: list[dict], rotation: list[dict]) -> str:
    fast = str(rotation[0].get("theme") or "") if rotation else "暂无显著轮动"
    long = str(themes[0].get("theme") or "") if themes else "暂无确认主线"
    return f"**当前最快轮动：{fast}；中长线首位：{long}。**"


def _rotation_table(rotation: list[dict]) -> list[str]:
    if not rotation:
        return ["暂无达到动量与宽度阈值的短周期轮动。"]
    lines = [
        "| 主题 | 状态 | 轮动分 | 5日涨幅 | 20日涨幅 | 5日上涨宽度 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in rotation:
        state = ROTATION_STATE_LABELS.get(str(item.get("rotation_state")), str(item.get("rotation_state") or "-"))
        lines.append(
            f"| **{item.get('theme', '')}** | {state} | {_fmt_num(item.get('rotation_score'))} | "
            f"{_fmt_pct_1(item.get('ret5'))} | {_fmt_pct_1(item.get('ret20'))} | "
            f"{_fmt_ratio(item.get('advancing_ratio_5d'))} |"
        )
    return lines


def _cluster_table(clusters: list[dict]) -> list[str]:
    if not clusters:
        return ["暂无满足多头排列与同日放量突破阈值的抱团共振。"]
    lines = [
        "| 主题 | 抱团分 | 多头排列占比 | 同频起跳 | 5日涨幅 |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in clusters:
        lines.append(
            f"| **{item.get('theme', '')}** | {_fmt_num(item.get('cluster_score'))} | "
            f"{_fmt_ratio(item.get('bull_alignment_ratio'))} | {int(item.get('sync_breakout_count') or 0)}只 | "
            f"{_fmt_pct_1(item.get('ret5'))} |"
        )
    return lines


def _theme_table(themes: list[dict]) -> list[str]:
    if not themes:
        return ["暂无分数超过阈值的中长线主题。"]
    lines = [
        "| 主题 | 状态 | 总分 | 龙头 | 热度 | 结构 | 宽度 | 持续 | 催化 | 拥挤 | 成分数 | 龙头数 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in themes:
        lines.append(
            f"| {item['theme']} | {STATE_LABELS.get(item['state'], item['state'])} | "
            f"{item['score']:.2f} | {item.get('leader_score', 0.0):.2f} | "
            f"{item['heat_score']:.2f} | {item['structure_score']:.2f} | "
            f"{item['breadth_score']:.2f} | {item['persistence_score']:.2f} | "
            f"{item['catalyst_score']:.2f} | {item['crowding_score']:.2f} | {item['member_count']} | "
            f"{item.get('leader_count', 0)} |"
        )
    return lines


def _theme_sections(themes: list[dict], grouped: dict[str, list[dict]]) -> list[str]:
    if not themes:
        return ["暂无分数超过阈值的中长线主题。"]
    lines: list[str] = []
    for item in themes:
        rows = grouped.get(str(item.get("theme", "") or ""), [])
        lines.extend(_theme_section(item, rows))
    return lines


def _theme_section(item: dict, candidates: list[dict]) -> list[str]:
    theme = str(item.get("theme", "") or "")
    lines = [
        f"### {theme} · {STATE_LABELS.get(item.get('state'), item.get('state', ''))}",
        "",
        (
            f"- 主线分: {_fmt_num(item.get('score'))}；龙头分: {_fmt_num(item.get('leader_score'))}；"
            f"宽度: {_fmt_num(item.get('breadth_score'))}；拥挤: {_fmt_num(item.get('crowding_score'))}；"
            f"成分: {int(item.get('member_count') or 0)}；龙头数: {int(item.get('leader_count') or 0)}"
        ),
        f"- 证据: {'; '.join(item.get('evidence') or ['暂无'])}",
        "",
    ]
    lines.extend(_candidate_table_for_theme(candidates))
    lines.append("")
    return lines


def _candidate_table_for_theme(candidates: list[dict]) -> list[str]:
    if not candidates:
        return ["暂无战略候选，说明主题强度或个股结构还不够。"]
    lines = [
        "| 排名 | 代码 | 名称 | 股票分 | 龙头分 | 60/120/250日涨幅 | 近120日新高 | 理由 |",
        "|---:|---|---|---:|---:|---:|---:|---|",
    ]
    for item in candidates[:8]:
        reasons = "; ".join(item.get("reasons", [])[:3])
        returns = f"{_fmt_pct(item.get('ret60'))}/{_fmt_pct(item.get('ret120'))}/{_fmt_pct(item.get('ret250'))}"
        lines.append(
            f"| {int(item.get('theme_rank') or 0)} | {item['code']} | {item['name']} | "
            f"{_fmt_num(item.get('stock_score'))} | {_fmt_num(item.get('leader_score'))} | "
            f"{returns} | {'是' if item.get('near_high_120d') else '否'} | {reasons} |"
        )
    return lines


def _orphan_candidate_sections(grouped: dict[str, list[dict]]) -> list[str]:
    lines: list[str] = []
    for theme, rows in grouped.items():
        lines.extend([f"### {theme}", ""])
        lines.extend(_candidate_table_for_theme(rows))
        lines.append("")
    return lines


def _candidates_by_theme(candidates: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in candidates:
        theme = str(item.get("theme", "") or "未分组").strip()
        grouped.setdefault(theme, []).append(item)
    for rows in grouped.values():
        rows.sort(key=lambda item: (int(item.get("theme_rank") or 999), -float(item.get("stock_score") or 0.0)))
    return grouped


def _rotation_board_html(themes: list[dict], rotation: list[dict]) -> str:
    fast = str(rotation[0].get("theme") or "") if rotation else "暂无显著轮动"
    long = str(themes[0].get("theme") or "") if themes else "暂无确认主线"
    cards = "".join(_rotation_card_html(item) for item in rotation)
    if not cards:
        cards = '<p class="empty">暂无达到动量与宽度阈值的短周期轮动。</p>'
    return f"""
    <section class="radar-verdict">
      <p class="kicker">FIRST READ</p>
      <h2>最快轮动：{_h(fast)}</h2>
      <p><strong>中长线首位：{_h(long)}</strong></p>
      <p class="gate-note">Shadow 只负责发现行情，不代表允许买入；正式操作仍需长期主题、买点、市场总闸和 OMS 共同通过。</p>
    </section>
    <section class="rotation-board">
      <div class="section-title"><p class="kicker">FAST ROTATION · SHADOW</p><h2>短周期轮动</h2></div>
      <div class="rotation-grid">{cards}</div>
    </section>
    <div class="section-title long-title"><p class="kicker">LONG HORIZON</p><h2>中长线主线</h2></div>
    """


def _rotation_card_html(item: dict) -> str:
    state = ROTATION_STATE_LABELS.get(str(item.get("rotation_state")), str(item.get("rotation_state") or "-"))
    return f"""
    <article class="rotation-card">
      <span class="rotation-state">{_h(state)}</span>
      <h3>{_h(item.get("theme"))}</h3>
      <strong class="rotation-score">{_fmt_num(item.get("rotation_score"))}</strong>
      <div class="rotation-facts">
        <span>5日 <b>{_fmt_pct_1(item.get("ret5"))}</b></span>
        <span>20日 <b>{_fmt_pct_1(item.get("ret20"))}</b></span>
        <span>上涨宽度 <b>{_fmt_ratio(item.get("advancing_ratio_5d"))}</b></span>
      </div>
    </article>
    """


def _theme_card_html(item: dict, candidates: list[dict]) -> str:
    theme = _h(item.get("theme"))
    evidence = "".join(f"<li>{_h(e)}</li>" for e in (item.get("evidence") or ["暂无"]))
    rows = "".join(_candidate_row_html(row) for row in candidates[:8]) or '<tr><td colspan="7">暂无战略候选</td></tr>'
    return f"""
    <section class="theme-card">
      <div class="theme-head">
        <div><p class="kicker">THEME LINE</p><h2>{theme}</h2></div>
        <span class="state">{_h(STATE_LABELS.get(item.get("state"), item.get("state", "")))}</span>
      </div>
      <div class="metrics">
        {_metric_html("主线分", item.get("score"))}
        {_metric_html("龙头分", item.get("leader_score"))}
        {_metric_html("宽度", item.get("breadth_score"))}
        {_metric_html("拥挤", item.get("crowding_score"))}
        {_metric_html("龙头数", item.get("leader_count"), decimals=0)}
      </div>
      <div class="evidence"><strong>证据</strong><ul>{evidence}</ul></div>
      <table><thead><tr><th>排名</th><th>代码</th><th>名称</th><th>股票分</th><th>龙头分</th><th>60/120/250日涨幅</th><th>新高</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """


def _orphan_card_html(theme: str, candidates: list[dict]) -> str:
    rows = "".join(_candidate_row_html(row) for row in candidates[:8])
    return f"""
    <section class="theme-card muted">
      <div class="theme-head"><div><p class="kicker">WATCHLIST</p><h2>{_h(theme)}</h2></div></div>
      <table><thead><tr><th>排名</th><th>代码</th><th>名称</th><th>股票分</th><th>龙头分</th><th>60/120/250日涨幅</th><th>新高</th></tr></thead><tbody>{rows}</tbody></table>
    </section>
    """


def _candidate_row_html(item: dict) -> str:
    returns = f"{_fmt_pct(item.get('ret60'))}/{_fmt_pct(item.get('ret120'))}/{_fmt_pct(item.get('ret250'))}"
    return (
        "<tr>"
        f"<td>{int(item.get('theme_rank') or 0)}</td>"
        f"<td><code>{_h(item.get('code'))}</code></td>"
        f"<td>{_h(item.get('name'))}</td>"
        f"<td>{_fmt_num(item.get('stock_score'))}</td>"
        f"<td>{_fmt_num(item.get('leader_score'))}</td>"
        f"<td>{_h(returns)}</td>"
        f"<td>{'是' if item.get('near_high_120d') else '否'}</td>"
        "</tr>"
    )


def _metric_html(label: str, value: object, *, decimals: int = 2) -> str:
    return f'<div class="metric"><span>{_h(label)}</span><strong>{_fmt_num(value, decimals)}</strong></div>'


def _theme_names(themes: list[dict]) -> set[str]:
    return {str(item.get("theme", "") or "") for item in themes}


def _fmt_num(value: object, decimals: int = 2) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct(value: object) -> str:
    try:
        return f"{float(value):+.0f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_pct_1(value: object) -> str:
    try:
        return f"{float(value):+.1f}%"
    except (TypeError, ValueError):
        return "-"


def _fmt_ratio(value: object) -> str:
    try:
        return f"{float(value):.0%}"
    except (TypeError, ValueError):
        return "-"


def _h(value: object) -> str:
    return escape(str(value or ""), quote=True)


def _html_shell(snapshot: dict, body: str) -> str:
    date_text = _h(snapshot.get("trade_date"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Theme Radar {date_text}</title>
  <style>{THEME_RADAR_CSS}</style>
</head>
<body>
  <main>
    <header class="hero">
      <p class="kicker">WYCKOFF THEME RADAR</p>
      <h1>主题与轮动雷达</h1>
      <p class="subtitle">先看短周期正在加速的方向，再看中长线确认；发现、买点和交易权限保持三层隔离。</p>
      <div class="date-chip">交易日 {date_text}</div>
    </header>
    <section class="layout">{body or '<p class="empty">暂无主线数据</p>'}</section>
  </main>
</body>
</html>
"""


THEME_RADAR_CSS = """
:root {
  --paper: #f7f2e8;
  --ink: #17201b;
  --muted: #68736c;
  --line: #d8cdbb;
  --teal: #176c68;
  --amber: #b87918;
  --red: #9f3d34;
  --panel: rgba(255, 252, 245, 0.86);
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background:
    linear-gradient(90deg, rgba(23,108,104,.08) 1px, transparent 1px),
    linear-gradient(0deg, rgba(23,108,104,.06) 1px, transparent 1px),
    var(--paper);
  background-size: 38px 38px;
  color: var(--ink);
  font-family: "Avenir Next", "PingFang SC", "Hiragino Sans GB", sans-serif;
}
main { width: min(1180px, calc(100vw - 48px)); margin: 0 auto; padding: 44px 0 64px; }
.hero { border-bottom: 2px solid var(--ink); padding-bottom: 24px; position: relative; }
.kicker { color: var(--teal); font-size: 12px; font-weight: 800; letter-spacing: .16em; margin: 0 0 10px; }
h1, h2 { font-family: Georgia, "Songti SC", serif; letter-spacing: 0; margin: 0; }
h1 { font-size: clamp(42px, 7vw, 88px); line-height: .9; max-width: 780px; }
h2 { font-size: 32px; }
.subtitle { max-width: 720px; color: var(--muted); font-size: 16px; line-height: 1.8; margin: 18px 0 0; }
.date-chip { position: absolute; right: 0; bottom: 26px; border: 1px solid var(--ink); padding: 10px 14px; font-weight: 700; }
.layout { display: grid; gap: 22px; margin-top: 28px; }
.radar-verdict { background: var(--ink); color: var(--paper); border-left: 8px solid var(--amber); padding: 24px 26px; }
.radar-verdict h2 { color: #fff; font-size: clamp(30px, 4vw, 48px); }
.radar-verdict > p:not(.kicker) { margin: 10px 0 0; }
.radar-verdict .kicker { color: #f3bd61; }
.gate-note { color: #d9ddd8; font-size: 14px; line-height: 1.7; }
.rotation-board { background: rgba(184,121,24,.10); border: 1px solid rgba(184,121,24,.42); padding: 22px; }
.section-title { margin-bottom: 16px; }
.section-title h2 { font-size: 34px; }
.long-title { border-top: 2px solid var(--ink); margin: 12px 0 -6px; padding-top: 22px; }
.rotation-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; }
.rotation-card { background: #fffaf0; border-top: 5px solid var(--amber); padding: 16px; position: relative; }
.rotation-card h3 { font-family: Georgia, "Songti SC", serif; font-size: 25px; margin: 8px 0 2px; }
.rotation-state { color: var(--amber); font-size: 12px; font-weight: 800; letter-spacing: .12em; }
.rotation-score { color: var(--red); font-size: 34px; line-height: 1; }
.rotation-facts { display: grid; gap: 4px; color: var(--muted); font-size: 13px; margin-top: 12px; }
.rotation-facts b { color: var(--ink); float: right; }
.theme-card { background: var(--panel); border: 1px solid var(--line); box-shadow: 8px 8px 0 rgba(23,32,27,.10); padding: 22px; }
.theme-card.muted { opacity: .86; }
.theme-head { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; border-bottom: 1px solid var(--line); padding-bottom: 16px; }
.state { background: var(--ink); color: var(--paper); padding: 7px 10px; font-size: 13px; font-weight: 800; white-space: nowrap; }
.metrics { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 18px 0; }
.metric { border-left: 3px solid var(--teal); background: rgba(23,108,104,.07); padding: 10px 12px; min-width: 0; }
.metric span { display: block; color: var(--muted); font-size: 12px; }
.metric strong { display: block; font-size: 24px; line-height: 1.1; margin-top: 4px; }
.evidence { display: grid; grid-template-columns: 54px 1fr; gap: 14px; color: var(--muted); margin-bottom: 18px; }
.evidence ul { margin: 0; padding-left: 18px; line-height: 1.7; }
table { width: 100%; border-collapse: collapse; font-size: 14px; background: rgba(255,255,255,.42); }
th, td { border-bottom: 1px solid var(--line); padding: 10px 8px; text-align: left; }
th { color: var(--teal); font-size: 12px; text-transform: uppercase; }
td:nth-child(1), td:nth-child(4), td:nth-child(5), td:nth-child(7) { text-align: right; }
code { color: var(--red); font-weight: 800; font-family: "SF Mono", Consolas, monospace; }
.empty { color: var(--muted); font-size: 18px; }
@media (max-width: 760px) {
  main { width: min(100vw - 24px, 1180px); padding-top: 24px; }
  .date-chip { position: static; display: inline-block; margin-top: 18px; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .theme-head, .evidence { display: block; }
  table { display: block; overflow-x: auto; white-space: nowrap; }
}
"""
