"""
工具注册表 — 按 Agent 工具族注册函数，去除 ADK 依赖。

核心思路：
1. ToolContext 复用 agents.tool_context（提供 .state / .state_lock 等共享字段）
2. 工具 JSON Schema 手动定义（比自动生成更可控）
3. 凭证通过 .env 环境变量提供
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any

from agents.tool_context import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具 Schema 定义（标准 JSON Schema，三家 Provider 通用）
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "search_stock_by_name",
        "description": "根据关键词搜索 A 股 / ETF / 美股 / 港股，支持名称、代码、常见中文别名和 TickFlow 标准代码。最多返回 10 条。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，如 '宁德'、'300750'、'纳指100'、'苹果'、'AAPL.US'、'00700.HK'",
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "analyze_stock",
        "description": "分析单只股票：A 股/ETF 支持 6 位代码；美股/港股使用 TickFlow 标准代码。支持 Wyckoff 健康诊断、近期行情或基本面质量查询。",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "股票代码，如 '000001'、'513100'、'AAPL.US'、'00700.HK'"},
                "mode": {
                    "type": "string",
                    "enum": ["diagnose", "price", "fundamental"],
                    "description": "'diagnose' 做 Wyckoff 结构化诊断；'price' 仅返回近期 OHLCV 行情；"
                    "'fundamental' 返回最新财报的基本面质量评级（研究性质，不改变正式漏斗或买卖信号）",
                },
                "cost": {"type": "number", "description": "持仓成本价（仅 diagnose 模式），默认 0"},
                "days": {"type": "integer", "description": "获取天数（仅 price 模式），默认 30，最大 250"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "portfolio",
        "description": "查看或诊断用户持仓。mode='view' 返回持仓列表和资金；mode='diagnose' 对每只持仓做 Wyckoff 健康诊断。",
        "parameters": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["view", "diagnose"],
                    "description": "'view' 仅查看持仓数据；'diagnose' 做持仓诊断",
                },
            },
        },
    },
    {
        "name": "get_market_overview",
        "description": "获取 A 股市场截面。可查最新或指定历史日期；返回主要指数，include_breadth=true 时同时返回全市上涨、下跌、平盘家数及涨跌幅分布。用于验证“指数下跌但个股上涨占优”等市场宽度问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "trade_date": {
                    "type": "string",
                    "description": "可选的历史日期，YYYY-MM-DD 或 YYYYMMDD；留空取最新交易日",
                },
                "include_breadth": {
                    "type": "boolean",
                    "description": "是否返回全市个股涨跌家数和分布；查询市场宽度时必须为 true",
                },
            },
        },
    },
    {
        "name": "get_market_history",
        "description": "回看 A 股主要指数过去 N 个交易日的日线量价关系，用于分析阶段位置、近期结构和量价变化。",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "回看交易日数量，默认 100，最大 320",
                },
                "index": {
                    "type": "string",
                    "description": "指数别名或代码，支持 sse/上证/csi300/沪深300/szse/深证/chinext/创业板",
                },
            },
        },
    },
    {
        "name": "screen_stocks",
        "description": "运行 Wyckoff 五层漏斗筛选，从市场中筛选结构性机会。聊天态默认快扫，明确要求全量时传 limit=0。",
        "parameters": {
            "type": "object",
            "properties": {
                "board": {
                    "type": "string",
                    "description": (
                        "股票池板块：'all'（全A股目标板块，含北交所）、"
                        "'main_chinext_star'（沪深主板+创业板+科创板，不含北交所）、"
                        "'main'（主板）、'chinext'（创业板）、'star'（科创板）、'bse'（北交所）"
                    ),
                },
                "style": {
                    "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                    "description": (
                        "可选候选风格偏好。支持 trend/strong/right（趋势强势）、"
                        "pullback/accum/left（低吸吸筹）、quality/stable（稳健质量）。"
                    ),
                },
                "theme": {
                    "type": "string",
                    "description": "可选主题偏好，如 机器人、芯片半导体、光模块、AI算力；只用于候选排序和摘要，不硬过滤。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 3000,
                    "description": "可选。默认由 agent 使用快扫预算；传正整数仅扫描前 N 只；传 0 表示全量扫描。",
                },
                "financial_metrics": {
                    "type": "boolean",
                    "description": "可选。聊天快扫默认跳过 TickFlow 财务指标以提升速度；明确需要财务过滤/完整复核时传 true。",
                },
            },
        },
    },
    {
        "name": "generate_ai_report",
        "description": "对指定股票列表生成威科夫三阵营 AI 深度研报（逻辑破产/储备营地/起跳板）。使用当前会话 LLM 配置，最多 10 只；不传 stock_codes 时会复用上一跳筛股候选。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_codes": {
                    "anyOf": [
                        {
                            "type": "array",
                            "items": {"anyOf": [{"type": "string"}, {"type": "object"}]},
                        },
                        {"type": "string"},
                    ],
                    "description": "可选。股票代码、逗号分隔代码，或候选对象列表；不传时复用上一跳筛股 handoff。",
                },
            },
        },
    },
    {
        "name": "generate_strategy_decision",
        "description": "综合持仓、候选标的和上一跳研报，生成去留决策（EXIT/TRIM/HOLD/PROBE/ATTACK）。使用当前会话 LLM 配置和持仓数据。",
        "parameters": {
            "type": "object",
            "properties": {
                "report_text": {"type": "string", "description": "可选，上一跳 AI 研报全文；不传则复用最近一次研报。"},
                "reviewed_codes": {
                    "anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                    "description": "可选，上一跳已复核股票代码列表，或逗号分隔代码字符串。",
                },
                "reviewed_symbols": {
                    "anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "object"}],
                    "description": "可选，上一跳已复核标的元数据列表，或单个候选对象。",
                },
                "screen_result": {"type": "object", "description": "可选，上一跳 screen_stocks 的结果。"},
            },
        },
    },
    {
        "name": "query_history",
        "description": "查询历史记录：形态复盘、信号确认池、策略归因运营摘要，或历史上下文归档。",
        "parameters": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["recommendation", "signal", "attribution", "archive"],
                    "description": (
                        "'recommendation' 形态复盘；'signal' 信号确认池；"
                        "'attribution' 策略归因治理器、latest_source/remote_error、latest_operator_summary、"
                        "latest_policy_display、latest_execution_summary、promotion_checklist 和 latest_operations；"
                        "'archive' 历史上下文归档"
                    ),
                },
                "status": {
                    "type": "string",
                    "description": "仅 signal：'all'/'pending'/'survived'/'confirmed'/'expired'",
                },
                "limit": {"type": "integer", "description": "返回记录数上限，默认 20"},
                "query": {"type": "string", "description": "仅 archive：搜索归档的关键词或股票代码"},
                "archive_ref": {
                    "type": "string",
                    "description": "仅 archive：要还原的具体归档引用链接（如 'archive://default/ctx_...'）",
                },
            },
            "required": ["source"],
        },
    },
    {
        "name": "evaluate_recommendation_events",
        "description": (
            "只读评估近期推荐/复盘股票在固定交易日窗口内的命中情况，输出排序接入判断、"
            "最新 policy picks 和候选质量字段。适合验证最近推荐池是否有可重点跟踪的股票。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "market": {
                    "type": "string",
                    "description": "市场，默认 cn；支持推荐追踪表已接入的市场标识。",
                },
                "horizon_days": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "评估未来交易日窗口，默认 5。",
                },
                "target_pct": {
                    "type": "number",
                    "minimum": 0.1,
                    "description": "窗口内目标涨幅百分比，默认 10。",
                },
                "max_dates": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "回看最近多少个推荐日期，默认 30。",
                },
                "kline_count": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "每只股票拉取的日线数量，默认 160。",
                },
                "top_k": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}},
                        {"type": "string"},
                        {"type": "integer"},
                    ],
                    "description": "可选 Top-K 集合，如 [1,3,5] 或 '1,3,5'。",
                },
            },
        },
    },
    {
        "name": "update_portfolio",
        "description": "管理用户持仓或删除追踪记录。操作后返回最新状态。",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "update", "remove", "set_cash", "delete_records"],
                    "description": "操作类型：add/update/remove/set_cash 管理持仓；delete_records 删除推荐或信号记录",
                },
                "code": {
                    "type": "string",
                    "description": "股票代码（add/update/remove 必填）：A股6位如601881，港股06881.HK，美股AAPL.US",
                },
                "name": {"type": "string", "description": "股票名称（可选）"},
                "shares": {"type": "integer", "description": "持仓股数"},
                "cost_price": {"type": "number", "description": "成本价"},
                "buy_dt": {"type": "string", "description": "买入日期（YYYYMMDD 格式）"},
                "free_cash": {"type": "number", "description": "可用资金（set_cash 时使用）"},
                "table": {"type": "string", "description": "仅 delete_records：'recommendation' 或 'signal'"},
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仅 delete_records：股票代码列表",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "record_trade_fill",
        "description": (
            "回填一笔已经发生的成交，按增量更新持仓与现金：摊薄成本价、扣佣金印花税、卖光时清仓、"
            "给出已实现盈亏。用户描述的是「已成交」就用这个，不要用 update_portfolio 覆盖快照。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "股票代码：A股6位 / 港股06881.HK / 美股AAPL.US",
                },
                "side": {"type": "string", "enum": ["buy", "sell"], "description": "成交方向"},
                "shares": {"type": "integer", "description": "成交股数（正数）"},
                "price": {"type": "number", "description": "成交价"},
                "trade_date": {"type": "string", "description": "成交日期 YYYYMMDD，缺省为今天"},
                "name": {"type": "string", "description": "股票名称（可选）"},
            },
            "required": ["code", "side", "shares", "price"],
        },
    },
    {
        "name": "check_background_tasks",
        "description": "查询后台任务执行状态。completed 任务会带 result_summary，用于继续读取扫描、研报、回测等异步结果摘要。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "run_backtest",
        "description": "回测威科夫五层漏斗策略的历史表现。耗时 3-10 分钟，后台执行。",
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "开始日期 YYYY-MM-DD，默认 6 个月前"},
                "end": {"type": "string", "description": "结束日期 YYYY-MM-DD，默认昨天"},
                "hold_days": {"type": "integer", "description": "最大持仓天数（5/10/15/30），默认 10"},
                "top_n": {"type": "integer", "description": "每日最大候选数，默认 4"},
                "board": {"type": "string", "description": "股票池：'all'/'main'/'chinext'/'star'"},
                "stop_loss_pct": {"type": "number", "description": "止损百分比（负数），默认 -8.0"},
                "take_profit_pct": {"type": "number", "description": "止盈百分比，默认 0.0"},
                "entry_price_mode": {
                    "type": "string",
                    "description": "入场成交价：'open'=T+1开盘价（默认）；'close'=T+1收盘价；'tail_1455'=T+1 14:55分钟线价",
                },
            },
        },
    },
    {
        "name": "ask_user_question",
        "description": (
            "向用户提出一个明确问题。模型应先根据上下文和工具判断；能合理推断的表述偏差、"
            "口语省略或术语混用不要提问，先按假设执行并说明；只有执行对象仍不明确，"
            "或需要写入/交易/高风险确认时使用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "向用户提问的问题描述文本（如：'这次回测要用哪个时间区间？'）",
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "提供给用户的单选项列表（例如：['近半年', '近一年']）",
                },
                "allow_free_text": {
                    "type": "boolean",
                    "description": "是否允许用户手动输入自定义回答。默认 true；纯确认场景可设为 false。",
                },
                "default_answer": {
                    "type": "string",
                    "description": "用户超时或直接回车时采用的默认回答，可为空。",
                },
            },
            "required": ["question"],
        },
    },
    {
        "name": "execute_skill",
        "description": "执行内置或用户自定义的高级投研技能（如 screen, checkup, report, strategy, backtest 等）。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名称，如 'screen'、'checkup'、'report'、'strategy'、'backtest'",
                },
                "user_input": {
                    "type": "string",
                    "description": "可选参数。如果技能包含 {user_input} 占位符，将替换为该值",
                },
            },
            "required": ["name"],
        },
    },
    # ── 委派工具 ──
    {
        "name": "delegate_to_research",
        "description": "委派研究员收集市场数据和情报。用于全市场扫描、信号查询、复盘记录、回测等数据收集任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "研究任务描述"},
                "context": {"type": "string", "description": "相关上下文信息（如持仓数据、大盘状态）"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "delegate_to_analysis",
        "description": "委派分析师做深度分析。用于个股诊断、持仓体检、AI 研报等需要 Wyckoff 框架深度分析的任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "分析任务描述"},
                "context": {"type": "string", "description": "相关上下文信息（如行情数据、大盘状态）"},
            },
            "required": ["task"],
        },
    },
    {
        "name": "delegate_to_trading",
        "description": "委派交易员做去留决策。用于持仓去留判断、攻防指令、调仓执行等交易决策任务。",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "交易决策任务描述"},
                "context": {"type": "string", "description": "相关上下文信息（如持仓列表、诊断结果）"},
            },
            "required": ["task"],
        },
    },
    # ── Agent 标准工具 ──
    {
        "name": "exec_command",
        "description": (
            "在用户本地执行 shell 命令并返回输出。可用于安装软件、查看系统状态、运行脚本等。"
            "命令会继承当前 CLI 进程环境变量；不要读取 .env 或密钥文件。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的 shell 命令"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 30，最大 120"},
                "cwd": {
                    "type": "string",
                    "description": "可选工作目录。用于在指定项目根目录执行命令，会经过本地路径安全校验。",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取用户本地文件内容。支持 txt/csv/json/xlsx 等格式。用户发来文件路径时使用此工具。CSV/Excel 自动解析为表格预览（前 50 行）。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（绝对路径或 ~ 开头）"},
                "encoding": {"type": "string", "description": "文件编码，默认 utf-8"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "将内容写入用户本地文件。自动创建父目录。可用于导出分析报告、保存数据等。",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
                "encoding": {"type": "string", "description": "文件编码，默认 utf-8"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "reassess_profile",
        "description": "基于已有的 AI 研报文本，重新评估并预览保守 (conservative)、均衡 (balanced) 或激进 (aggressive) 决策风格下的交易信号与参数调整。不写入数据库。",
        "parameters": {
            "type": "object",
            "properties": {
                "report_text": {
                    "type": "string",
                    "description": "研报文本内容（通常为 Markdown）",
                },
                "profile": {
                    "type": "string",
                    "enum": ["conservative", "balanced", "aggressive"],
                    "description": "决策风格：conservative(保守), balanced(均衡), aggressive(激进)",
                },
            },
            "required": ["report_text", "profile"],
        },
    },
    {
        "name": "diagnose_backend",
        "description": "运行后端大模型诊疗（Doctor），全面检查所有 LLM 接口和数据源凭证（如 Tushare Token）的配置、连通性及延迟，输出诊断报告。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "browser_research",
        "description": (
            "通过本机 Chrome CDP 搜索公开网页并抽取正文，返回可引用的标题/链接/摘要。"
            "适合未上市标的、IPO、舆情等公开信息检索。需先启动带 --remote-debugging-port 的 Chrome。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，如公司名、IPO、事件主题"},
                "max_results": {"type": "integer", "description": "搜索结果条数，默认 5，最大 10"},
                "max_pages": {"type": "integer", "description": "深入打开的页面数，默认 3，最大 5"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "research_hypothesis",
        "description": (
            "管理可追溯的策略研究假设，并关联回测、归因、shadow 或观察证据。"
            "用于记录为什么测试某条规则、何时验证通过以及什么条件会使它失效。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "list", "detail", "update", "link_evidence", "evaluate", "transition"],
                },
                "hypothesis_id": {"type": "string"},
                "title": {"type": "string"},
                "thesis": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["exploring", "testing", "validated", "monitoring", "rejected"],
                },
                "universe": {"type": "string"},
                "signal_definition": {"type": "string"},
                "invalidation_criteria": {"type": "string"},
                "evidence_type": {
                    "type": "string",
                    "enum": ["backtest", "stability", "attribution", "shadow", "report", "observation"],
                },
                "artifact_ref": {"type": "string"},
                "verdict": {"type": "string", "enum": ["pass", "review", "fail"]},
                "summary": {"type": "string"},
                "metrics": {"type": "object"},
                "target_status": {
                    "type": "string",
                    "enum": ["exploring", "testing", "validated", "monitoring", "rejected"],
                },
                "reason": {"type": "string", "description": "状态迁移原因；拒绝或重新探索时必填。"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["action"],
        },
    },
]


@dataclass(frozen=True)
class ToolSpec:
    """Runtime behavior metadata for one tool."""

    name: str
    display_name: str
    concurrency_safe: bool = False
    requires_approval: bool = False
    background: bool = False


# 工具行为元数据：runtime / TUI / 执行器都从这里派生策略。
TOOL_SPECS: dict[str, ToolSpec] = {
    "search_stock_by_name": ToolSpec("search_stock_by_name", "搜索股票", concurrency_safe=True),
    "analyze_stock": ToolSpec("analyze_stock", "个股分析", concurrency_safe=True),
    "portfolio": ToolSpec("portfolio", "持仓", concurrency_safe=True),
    "get_market_overview": ToolSpec("get_market_overview", "大盘水温", concurrency_safe=True),
    "get_market_history": ToolSpec("get_market_history", "大盘回看", concurrency_safe=True),
    "screen_stocks": ToolSpec("screen_stocks", "全市场扫描", background=True),
    "generate_ai_report": ToolSpec("generate_ai_report", "深度审讯", background=True),
    "generate_strategy_decision": ToolSpec("generate_strategy_decision", "攻防决策", background=True),
    "query_history": ToolSpec("query_history", "历史查询", concurrency_safe=True),
    "evaluate_recommendation_events": ToolSpec("evaluate_recommendation_events", "推荐评估", background=True),
    "research_hypothesis": ToolSpec("research_hypothesis", "研究假设", concurrency_safe=True),
    "update_portfolio": ToolSpec("update_portfolio", "调仓操作", requires_approval=True),
    "record_trade_fill": ToolSpec("record_trade_fill", "成交回填", requires_approval=True),
    "run_backtest": ToolSpec("run_backtest", "回测", background=True),
    "check_background_tasks": ToolSpec("check_background_tasks", "任务状态"),
    "exec_command": ToolSpec("exec_command", "执行命令", requires_approval=True),
    "read_file": ToolSpec("read_file", "读取文件"),
    "write_file": ToolSpec("write_file", "写入文件", requires_approval=True),
    "browser_research": ToolSpec("browser_research", "浏览器搜索"),
    "reassess_profile": ToolSpec("reassess_profile", "风控评估", concurrency_safe=True),
    "diagnose_backend": ToolSpec("diagnose_backend", "大模型诊疗", concurrency_safe=True),
    "ask_user_question": ToolSpec("ask_user_question", "提问用户", concurrency_safe=False),
    "execute_skill": ToolSpec("execute_skill", "执行技能", concurrency_safe=True),
    "delegate_to_research": ToolSpec("delegate_to_research", "委派研究员"),
    "delegate_to_analysis": ToolSpec("delegate_to_analysis", "委派分析师"),
    "delegate_to_trading": ToolSpec("delegate_to_trading", "委派交易员"),
}

# 兼容旧调用点；新增代码优先使用 ToolSpec / ToolRegistry 方法。
BACKGROUND_TOOLS = {name for name, spec in TOOL_SPECS.items() if spec.background}
CONFIRM_TOOLS = {name for name, spec in TOOL_SPECS.items() if spec.requires_approval}
CONCURRENCY_SAFE_TOOLS = {name for name, spec in TOOL_SPECS.items() if spec.concurrency_safe}
TOOL_DISPLAY_NAMES: dict[str, str] = {name: spec.display_name for name, spec in TOOL_SPECS.items()}


def tool_spec(name: str) -> ToolSpec | None:
    """Return metadata for a registered tool name."""

    return TOOL_SPECS.get(name)


def is_concurrency_safe(name: str) -> bool:
    """Return whether a tool can safely run in a concurrent batch."""

    spec = tool_spec(name)
    return bool(spec and spec.concurrency_safe)


ASK_USER_TIMEOUT_SENTINEL = "__ask_user_question_timeout__"


def ask_user_question(
    question: str,
    options: list[str] | None = None,
    allow_free_text: bool = True,
    default_answer: str = "",
    *,
    tool_context=None,
) -> dict[str, Any]:
    """向用户提问并阻塞等待答复。"""
    registry = getattr(tool_context, "registry", None) if tool_context else None
    if registry and getattr(registry, "_ask_user_question_callback", None):
        try:
            answer = registry._ask_user_question_callback(question, options, allow_free_text, default_answer)
            if answer == ASK_USER_TIMEOUT_SENTINEL:
                # 超时不是答复：原先它会以 status="answered" 返回「已超时未作答」这句话本身，
                # 模型只能把这句话当成用户的回答内容继续往下推理。
                return {
                    "status": "timeout",
                    "error": (
                        "提问等待超时，用户没有作答——这既不是答复也不是拒绝。"
                        "不要把超时当成用户的回答内容，也不要重复追问同一个问题；"
                        "请直接说明未收到答复以及需要用户补充什么。"
                    ),
                }
            return {"status": "answered", "answer": answer, "result": f"用户已答复: {answer}"}
        except Exception as e:
            logger.error("ask_user_question_callback failed", exc_info=True)
            return {"error": f"无法获取用户答复: {e}"}

    # Headless fallback: stdin
    print(f"\n💬 Agent 提问: {question}")
    if options:
        for i, opt in enumerate(options):
            print(f"  [{i}] {opt}")
    try:
        prompt = "请输入回答"
        if default_answer:
            prompt += f"（默认: {default_answer}）"
        val = input(f"{prompt}: ").strip() or default_answer
        if options and val.isdigit():
            idx = int(val)
            if 0 <= idx < len(options):
                val = options[idx]
        if options and not allow_free_text and val not in options:
            return {"error": "用户回答不在可选项内"}
        return {"status": "answered", "answer": val, "result": f"用户已答复: {val}"}
    except Exception as e:
        return {"error": f"获取命令行答复失败: {e}"}


def execute_skill(name: str, user_input: str = "", *, tool_context=None) -> dict[str, Any]:
    """执行内置或用户自定义的技能，将技能 prompt 作为结果返回供模型后续消费。"""
    from cli.skills import load_skills

    skills = load_skills()
    skill = skills.get(name)
    if not skill:
        return {"error": f"未知技能: {name}"}

    prompt = skill.prompt.replace("{user_input}", user_input).strip()
    return {
        "status": "success",
        "skill": name,
        "instructions": prompt,
        "message": f"技能 {name} 已成功加载。请严格按照以下 instructions 执行：",
    }


# ---------------------------------------------------------------------------
class ToolRegistry:
    """工具注册表：注册、查询 schema、执行工具。"""

    def __init__(self, user_id: str = "", access_token: str = "", refresh_token: str = ""):
        self._tool_context = ToolContext(
            state={
                "user_id": user_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
            }
        )
        self._tool_context.registry = self
        self._tools = self._register_tools()
        self._bg_manager = None
        self._on_bg_complete = None
        self._confirm_callback = None
        self._ask_user_question_callback = None
        self._always_allowed: set[str] = set()

        # Initialize ToolSurface and populate from TOOL_SCHEMAS
        from tools.tool_surface import ToolSurface, from_json_schema

        self._tool_surface = ToolSurface()
        for schema in TOOL_SCHEMAS:
            name = schema["name"]
            handler = self._tools.get(name)
            if handler:
                try:
                    tool_def = from_json_schema(schema, handler)
                    self._tool_surface.register(tool_def)
                except Exception as e:
                    logger.warning("Failed to register tool %s to tool_surface: %s", name, e)

    def set_provider(self, provider):
        """注入 LLM Provider，供委派工具启动 sub-agent。"""
        self._tool_context.provider = provider

    def set_confirm_callback(self, callback):
        """注入确认回调，高风险工具执行前会调用。callback(name, args) -> dict。"""
        self._confirm_callback = callback

    def set_ask_user_question_callback(self, callback):
        """注入 ask_user_question 回调。"""
        self._ask_user_question_callback = callback

    def set_background_manager(self, bg_manager, on_complete=None):
        from cli.background import BackgroundTaskManager

        self._bg_manager: BackgroundTaskManager = bg_manager
        self._on_bg_complete = on_complete

    @property
    def state(self) -> dict:
        """统一的 session state，__main__ 和工具共享同一份。"""
        return self._tool_context.state

    def _register_tools(self) -> dict[str, callable]:
        """注册所有工具函数。"""
        from agents.backtest_tools import run_backtest
        from agents.browser_tools import browser_research
        from agents.diagnosis_tools import analyze_stock
        from agents.history_tools import query_history
        from agents.local_tools import exec_command, read_file, write_file
        from agents.market_tools import get_market_history, get_market_overview
        from agents.portfolio_tools import portfolio, record_trade_fill, update_portfolio
        from agents.recommendation_tools import evaluate_recommendation_events
        from agents.report_tools import generate_ai_report
        from agents.research_tools import research_hypothesis
        from agents.screen_tools import screen_stocks
        from agents.search_tools import search_stock_by_name
        from agents.strategy_tools import generate_strategy_decision
        from cli.sub_agents import (
            delegate_to_analysis,
            delegate_to_research,
            delegate_to_trading,
        )
        from tools.backend_doctor import diagnose_backend
        from workflows.reassess_profile import reassess_decision_profile

        return {
            "search_stock_by_name": search_stock_by_name,
            "analyze_stock": analyze_stock,
            "portfolio": portfolio,
            "get_market_overview": get_market_overview,
            "get_market_history": get_market_history,
            "screen_stocks": screen_stocks,
            "generate_ai_report": generate_ai_report,
            "generate_strategy_decision": generate_strategy_decision,
            "query_history": query_history,
            "evaluate_recommendation_events": evaluate_recommendation_events,
            "research_hypothesis": research_hypothesis,
            "update_portfolio": update_portfolio,
            "record_trade_fill": record_trade_fill,
            "run_backtest": run_backtest,
            "ask_user_question": ask_user_question,
            "execute_skill": execute_skill,
            "delegate_to_research": delegate_to_research,
            "delegate_to_analysis": delegate_to_analysis,
            "delegate_to_trading": delegate_to_trading,
            "exec_command": exec_command,
            "read_file": read_file,
            "write_file": write_file,
            "browser_research": browser_research,
            "reassess_profile": reassess_decision_profile,
            "diagnose_backend": diagnose_backend,
        }

    def schemas(self, allowed_tools: set[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """返回工具 JSON Schema；allowed_tools 存在时只暴露当前 workflow 范围。"""
        if not allowed_tools:
            return TOOL_SCHEMAS
        allowed = set(allowed_tools)
        return [schema for schema in TOOL_SCHEMAS if schema["name"] in allowed]

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def prepare(self, name: str, args: dict[str, Any]) -> Any:
        """Pre-execution gates (exists + schema/scope). Approval stays in execute()."""

        from cli.prepare_tool_call import accept, reject

        if name == "check_background_tasks":
            return accept(args)
        if name not in self._tools:
            return reject("tool_not_found", f"未知工具: {name}", args=args)
        if self._tool_surface.resolve(name) is None:
            return accept(args)
        prepared = self._tool_surface.prepare_call(name, args)
        if prepared.get("ok"):
            return accept(prepared.get("args") or args)
        return reject(
            str(prepared.get("code") or "invalid_arguments"),
            str(prepared.get("message") or "工具参数校验失败"),
            args=args if isinstance(args, dict) else {},
            details=prepared.get("details") if isinstance(prepared.get("details"), dict) else None,
        )

    def _check_user_confirmed_in_history(self, messages: list[dict[str, Any]] | None) -> bool:
        if not messages:
            return False
        for m in reversed(messages):
            if m.get("role") == "tool" and m.get("name") == "ask_user_question":
                content = m.get("content", "")
                lower_content = content.lower()
                if any(
                    word in lower_content
                    for word in ("确认", "允许", "继续", "执行", "yes", "ok", "allow", "confirm", "opt_0")
                ):
                    return True
        return False

    def execute(self, name: str, args: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> Any:
        """执行指定工具，返回结果。长任务自动提交后台。"""
        # check_background_tasks 直接返回状态
        if name == "check_background_tasks":
            if not self._bg_manager:
                return {"tasks": [], "message": "无后台任务"}
            self._remember_background_handoffs()
            return {"tasks": self._bg_manager.list_tasks()}

        fn = self._tools.get(name)
        if fn is None:
            return {"error": f"未知工具: {name}"}

        args, blocked = self._confirm_high_risk_call(name, args, messages)
        if blocked:
            return blocked

        # 用副本注入 tool_context，避免污染原始 args（会被序列化进 messages）
        call_args = dict(args)
        sig = inspect.signature(fn)
        if "tool_context" in sig.parameters:
            call_args["tool_context"] = self._tool_context

        # 长任务提交后台
        if self.is_background(name) and self._bg_manager is not None:
            task_id = f"bg_{time.time_ns()}_{name}"
            display = self.display_name(name)
            self._bg_manager.submit(
                task_id,
                name,
                fn,
                call_args,
                on_complete=self._on_bg_complete,
            )
            return {
                "status": "background",
                "task_id": task_id,
                "message": f"{display}已提交后台执行，您可以继续提问。任务完成后会自动通知。",
            }

        if self._tool_surface.resolve(name) is None:
            try:
                return fn(**call_args)
            except Exception as e:
                logger.exception("Tool %s execution failed", name)
                return {"error": f"工具执行失败: {e}"}

        from cli.auth import get_tool_timeout_seconds
        from tools.tool_surface import ToolAccessContext

        ctx = ToolAccessContext(
            timeout_seconds=get_tool_timeout_seconds(),
            session_id=self._tool_context.state.get("session_id"),
        )
        res = self._tool_surface.execute_tool(name, call_args, ctx)
        if not res["ok"]:
            err = res["error"]
            return {"error": err["message"], "code": err["code"]}
        return res["result"]

    def _remember_background_handoffs(self) -> None:
        for _task_id, tool_name, result in self._bg_manager.completed_results():
            self.remember_tool_handoff(tool_name, result)

    def wait_background_tasks(self, task_ids: list[str], timeout_seconds: float = 30.0) -> list[dict[str, Any]]:
        if not self._bg_manager:
            return []
        statuses = self._bg_manager.wait_for_tasks(task_ids, timeout_seconds=timeout_seconds)
        self._remember_background_handoffs()
        return statuses

    def remember_tool_handoff(self, tool_name: str, result: Any) -> None:
        """Restore session handoff state from a completed tool result."""

        if not isinstance(result, dict) or result.get("error"):
            return
        if tool_name == "screen_stocks" or result.get("job_kind") == "funnel_screen":
            from agents.screen_tools import remember_screen_handoff

            remember_screen_handoff(self._tool_context, result)
        elif tool_name == "analyze_stock":
            from agents.diagnosis_tools import remember_stock_diagnosis

            remember_stock_diagnosis(self._tool_context, result)
        elif tool_name == "generate_ai_report":
            from agents.report_tools import remember_ai_report

            remember_ai_report(self._tool_context, result)
        elif tool_name == "generate_strategy_decision":
            from agents.strategy_tools import remember_strategy_decision

            remember_strategy_decision(self._tool_context, result)
        elif tool_name == "evaluate_recommendation_events" or result.get("job_kind") == "recommendation_event_eval":
            from agents.recommendation_tools import remember_recommendation_event_eval

            remember_recommendation_event_eval(self._tool_context, result)

    def _confirm_high_risk_call(
        self,
        name: str,
        args: dict[str, Any],
        messages: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], dict[str, str] | None]:
        if not self.requires_approval(name) or name in self._always_allowed:
            return args, None
        if self._check_user_confirmed_in_history(messages):
            return args, None
        if not self._confirm_callback:
            return args, {
                "error": (
                    f"操作 [{name}] 具有高风险或破坏性参数，已被拦截。 "
                    "你必须先调用 `ask_user_question` 工具向用户解释其风险并获取显式确认（如单选项或回复“确认”），"
                    "在用户确认后你才可以再次提交此操作。"
                )
            }
        confirm = self._confirm_callback(name, args)
        action = confirm.get("action", "deny")
        if action == "timeout":
            # 超时和明确拒绝必须分开：说成「用户拒绝」等于伪造一件没发生的事，模型只能照着
            # 这个措辞往下写，用户会在回复里读到自己从没做过的决定。
            return args, {
                "error": (
                    f"操作 [{name}] 的确认弹窗等待超时，用户没有做出选择——这不是拒绝。"
                    "不要声称用户拒绝或取消了操作。请说明确认超时、该操作尚未执行，"
                    "并让用户确认后重试。"
                )
            }
        if action == "deny":
            return args, {"error": "用户拒绝执行此操作"}
        if action == "always":
            self._always_allowed.add(name)
        if action == "edit":
            return confirm.get("modified_args", args), None
        return args, None

    def display_name(self, name: str) -> str:
        """返回工具的中文显示名。"""
        spec = self.spec(name)
        return spec.display_name if spec else name

    def spec(self, name: str) -> ToolSpec | None:
        """返回工具行为元数据。"""
        return tool_spec(name)

    def concurrency_safe(self, name: str) -> bool:
        """返回工具是否可安全并行执行。"""
        return is_concurrency_safe(name)

    def requires_approval(self, name: str) -> bool:
        """返回工具执行前是否需要用户确认。"""
        spec = self.spec(name)
        return bool(spec and spec.requires_approval)

    def is_background(self, name: str) -> bool:
        """返回工具是否应提交后台执行。"""
        spec = self.spec(name)
        return bool(spec and spec.background)
