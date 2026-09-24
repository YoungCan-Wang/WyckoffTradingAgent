"""Versioned public tool contracts, independent of business imports and the MCP SDK."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

CONTRACT_VERSION = "1.1"
_UNSET = object()


def param(kind: str, default: Any = _UNSET, **constraints: Any) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": kind, **constraints}
    if default is not _UNSET:
        schema["default"] = default
        if default is None:
            schema["type"] = [kind, "null"]
    return schema


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: str
    description: str
    parameters: dict[str, dict[str, Any]] = field(default_factory=dict)
    read_only: bool = False
    writes: bool = False
    timeout: float = 60.0
    open_world: bool = True

    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": deepcopy(self.parameters),
            "required": [name for name, value in self.parameters.items() if "default" not in value],
            "additionalProperties": False,
        }

    def descriptor(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema(),
            "outputSchema": {"type": "object", "additionalProperties": True},
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": not self.read_only,
                "idempotentHint": False,
                "openWorldHint": self.open_world,
            },
        }

    def arguments(self, supplied: dict[str, Any]) -> dict[str, Any]:
        defaults = {name: deepcopy(value["default"]) for name, value in self.parameters.items() if "default" in value}
        return defaults | deepcopy(supplied)


_BOARD = ["all", "main_chinext", "main", "chinext", "star", "bse"]
_LOCAL = "agents.public_mcp_handlers:"

TOOLS = (
    ToolSpec(
        "scan_corporate_events",
        "agents.corporate_event_tools:scan_corporate_events",
        "检索最近48小时重组/停复牌消息；返回来源状态，否认/终止与未知时间单独标识。不是全量公告核对，不构成买卖许可。",
        {"limit": param("integer", 20, minimum=1, maximum=50)},
        read_only=True,
        timeout=60.0,
    ),
    ToolSpec(
        "query_history",
        "agents.history_tools:query_history",
        "查询形态复盘、信号池或策略归因。历史记录不是当前可执行买单。",
        {
            "source": param("string", enum=["recommendation", "signal", "attribution"]),
            "status": param("string", "all"),
            "limit": param("integer", 20, minimum=1, maximum=1000),
        },
        read_only=True,
    ),
    ToolSpec(
        "research_hypothesis",
        "agents.research_tools:research_hypothesis",
        "查询或维护研究假设与证据台账。仅 list/detail 默认可用；其他操作需要显式开启 MCP 写入。",
        {
            "action": param(
                "string", enum=["create", "list", "detail", "update", "link_evidence", "evaluate", "transition"]
            ),
            **{
                name: param("string", "")
                for name in (
                    "hypothesis_id",
                    "title",
                    "thesis",
                    "status",
                    "universe",
                    "signal_definition",
                    "invalidation_criteria",
                    "evidence_type",
                    "artifact_ref",
                    "summary",
                    "target_status",
                    "reason",
                )
            },
            "verdict": param("string", "review"),
            "metrics": param("object", None),
            "limit": param("integer", 50, minimum=1, maximum=1000),
        },
    ),
    ToolSpec(
        "search_stock_by_name",
        "agents.search_tools:search_stock_by_name",
        "按股票名称、代码或拼音首字母搜索 A 股。结果数组放在 structuredContent.result。",
        {"keyword": param("string", minLength=1, maxLength=200)},
        read_only=True,
    ),
    ToolSpec(
        "analyze_stock",
        "agents.diagnosis_tools:analyze_stock",
        "分析单只 A 股。diagnose=威科夫结构；price=OHLCV；fundamental=基本面研究，不改变正式买卖信号。",
        {
            "code": param("string", minLength=1, maxLength=32),
            "mode": param("string", "diagnose", enum=["diagnose", "price", "fundamental"]),
            "cost": param("number", 0.0, minimum=0),
            "days": param("integer", 30, minimum=1),
        },
    ),
    ToolSpec(
        "get_market_overview",
        "agents.market_tools:get_market_overview",
        "获取最新或指定日期的 A 股市场截面；trade_date 接受 YYYY-MM-DD 或 YYYYMMDD。",
        {"trade_date": param("string", ""), "include_breadth": param("boolean", False)},
        read_only=True,
    ),
    ToolSpec(
        "screen_stocks",
        "agents.screen_tools:screen_stocks",
        "运行选股漏斗。limit 留空为聊天快扫，0 为全量；研究候选不等于 BUY。返回分层计数、代码索引和有界预览。",
        {
            "board": param("string", "all", enum=_BOARD),
            "limit": param("integer", None, minimum=0, maximum=3000),
            "financial_metrics": param("boolean", None),
        },
        timeout=600.0,
    ),
    ToolSpec(
        "run_backtest",
        "agents.backtest_tools:run_backtest",
        "回测生产漏斗，可能耗时 3–10 分钟。open=信号次日开盘；close=次日收盘；tail_1455=次日14:55。",
        {
            "start": param("string", ""),
            "end": param("string", ""),
            "hold_days": param("integer", 10, minimum=1),
            "top_n": param("integer", 3, minimum=1),
            "board": param("string", "all"),
            "stop_loss_pct": param("number", -7.0),
            "take_profit_pct": param("number", 18.0),
            "entry_price_mode": param("string", "open", enum=["open", "close", "tail_1455"]),
        },
        timeout=600.0,
    ),
    ToolSpec("market_regime", "agents.engine_tools:market_regime", "计算 A 股市场状态和动态阈值，不调用 LLM。"),
    ToolSpec(
        "wyckoff_diagnose",
        "agents.engine_tools:wyckoff_diagnose",
        "返回单股威科夫交易区间、阶段及 Spring/SOS/LPS/EVR 信号，不调用 LLM。",
        {"code": param("string", minLength=1, maxLength=32)},
    ),
    ToolSpec(
        "intraday_analysis",
        "agents.engine_tools:intraday_analysis",
        "返回单股分钟级 VWAP、趋势、动量和量能特征，不调用 LLM。",
        {"code": param("string", minLength=1, maxLength=32)},
    ),
    ToolSpec(
        "intraday_rescue_check",
        "agents.engine_tools:intraday_rescue_check",
        "评估单股 60 分钟结构、平台突破和 VWAP 收复，不等于下单许可。",
        {"code": param("string", minLength=1, maxLength=32)},
    ),
    ToolSpec(
        "run_funnel_simulation",
        _LOCAL + "run_funnel_simulation",
        "运行生产漏斗并返回结构数据，不推送通知。limit 留空或 0 为全量；保留生产风控与执行边界。",
        {"board": param("string", "all", enum=_BOARD), "limit": param("integer", None, minimum=0, maximum=3000)},
        timeout=600.0,
    ),
    ToolSpec(
        "portfolio",
        "agents.portfolio_tools:portfolio",
        "读取或诊断当前本地用户的持仓；云端数据需要该用户的登录态。不是公开多租户接口。",
        {"mode": param("string", "view", enum=["view", "diagnose"])},
        timeout=300.0,
    ),
    ToolSpec(
        "update_portfolio",
        "agents.portfolio_tools:update_portfolio",
        "修改持仓或追踪记录，默认拒绝。add 必须明确 buy_dt；set_cash 必须明确 free_cash，省略不代表零。",
        {
            "action": param("string", enum=["add", "remove", "update", "set_cash", "delete_records"]),
            **{name: param("string", "") for name in ("code", "name", "buy_dt", "table")},
            "shares": param("integer", 0, minimum=0),
            "cost_price": param("number", 0, minimum=0),
            "free_cash": param("number", None),
            "codes": param("array", None, items={"type": "string"}),
            "items": param("array", None, items={"type": "object"}),
        },
        writes=True,
    ),
    ToolSpec(
        "record_trade_fill",
        "agents.portfolio_tools:record_trade_fill",
        "回填已经发生的真实成交，增量更新持仓与现金。默认拒绝，不得自动重试。",
        {
            "code": param("string", minLength=1, maxLength=32),
            "side": param("string", enum=["buy", "sell"]),
            "shares": param("integer", minimum=1),
            "price": param("number", exclusiveMinimum=0),
            "trade_date": param("string", ""),
            "name": param("string", ""),
        },
        writes=True,
    ),
    ToolSpec(
        "generate_ai_report",
        "agents.report_tools:generate_ai_report",
        "为指定股票生成 AI 研报，会消耗用户模型配额，可能保存研究产物。不是投资收益保证。",
        {"stock_codes": param("array", minItems=1, maxItems=100, items={"type": "string", "minLength": 1})},
        timeout=300.0,
    ),
    ToolSpec(
        "generate_strategy_decision",
        "agents.strategy_tools:generate_strategy_decision",
        "根据当前用户持仓生成策略研究，会消耗模型配额；不执行交易。",
        timeout=300.0,
    ),
    ToolSpec(
        "reassess_profile",
        "workflows.reassess_profile:reassess_decision_profile",
        "按保守、均衡或激进风格重新评估已有研报；仅预览，不写数据库。",
        {
            "report_text": param("string", minLength=1),
            "profile": param("string", "balanced", enum=["conservative", "balanced", "aggressive"]),
        },
        read_only=True,
    ),
    ToolSpec(
        "diagnose_backend",
        "tools.backend_doctor:diagnose_backend",
        "检查本地数据源与模型后端配置和连通性，可能访问第三方服务。不得把凭证当作返回数据。",
        timeout=120.0,
    ),
)
TOOL_BY_NAME = {tool.name: tool for tool in TOOLS}
if len(TOOL_BY_NAME) != len(TOOLS):
    raise ValueError("Duplicate public MCP tool name")
