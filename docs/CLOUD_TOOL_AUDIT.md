# 云端工具审计

目的：判断 `ToolRegistry` 里的 34 个 agent 工具哪些能放进**共用的云端进程**（会员 Agent 车道，见
[ARCHITECTURE.md](ARCHITECTURE.md) 的「会员车道 Agent Runtime 服务」）。这些工具按本机桌面设计：
本机文件、本机 SQLite、环境变量里的密钥、本机 Chrome、直接跑命令。

## 怎么跑

```bash
.venv/bin/python scripts/audit_cloud_tools.py            # 逐工具判定
.venv/bin/python scripts/audit_cloud_tools.py --chains   # 带调用链证据
.venv/bin/python scripts/audit_cloud_tools.py --tool analyze_stock
```

这是**静态、尽力而为**的分析：只追得到能解析的调用（模块级函数、`from x import y`、`mod.func`，含函数内
延迟导入）；`obj.method()` 这类动态分派追不到。「追不到的调用」一列主要是 pandas 方法调用的噪声，不要
当结论。脚本只能用来排除风险和定位要改的地方，**不能证明某个工具安全**，也看不见 `if has_cloud(...)`
这类条件分支。

## 判定

| 判定 | 含义 |
|---|---|
| `sandbox-only` | 子进程或浏览器：共用进程里绝不能跑，只能进远端沙箱 |
| `needs-fix` | 碰本机 SQLite / 家目录 / 环境变量写入，需要改成按请求隔离后才能放行 |
| `review` | 只读环境变量或本机文件（多为缓存、配置），需要人工确认 |
| `candidate` | 没追到效应；**不等于安全**（`delegate_to_*` 会再经注册表调用工具，属于动态分派） |

## 结果（34 个工具）

- **sandbox-only**：`exec_command`、`browser_research`、`app_browser`、`annotate_chart`
- **needs-fix**：`portfolio`、`update_portfolio`、`set_stop_loss`、`record_trade_fill`、`query_history`、
  `research_hypothesis`、`run_backtest`、`screen_stocks`、`generate_ai_report`、`generate_strategy_decision`、
  `analyze_stock`、`get_market_overview`、`get_market_history`、`execute_skill`、`read_file`、`write_file`
- **review**：`search_stock_by_name`、`market_regime`、`wyckoff_diagnose`、`intraday_analysis`、
  `intraday_rescue_check`、`evaluate_recommendation_events`、`render_dashboard`、`save_report`、`diagnose_backend`
- **candidate**：`ask_user_question`、`delegate_to_analysis`、`delegate_to_research`、`delegate_to_trading`、
  `reassess_profile`

没有任何数据类工具是「干净」的。

## 关键发现

1. **`analyze_stock`、`get_market_overview`、`get_market_history` 的环境变量、文件、写环境变量效应，全部汇到同一个
   咽喉点**：`agents/tool_context.py` 的 `ensure_tushare_token → get_credential`。其中写 `os.environ` 和回落本机
   配置都被 `has_cloud(tool_context)`（上下文带 `access_token`）挡掉，静态分析看不出这个条件，需要人工核实。
2. **真正的问题是权限，不是文件**：云模式下 `get_credential` 走 `load_user_credentials` →
   `load_user_settings_admin` → `create_admin_client`，要求运行环境里有 `SUPABASE_SERVICE_ROLE_KEY`。共用容器
   不应该持有这把钥匙：任何一处把 `user_id` 传错，就能读到别人的设置和密钥。
3. 建议的方向：由网关（它本来就能用用户令牌读 `user_settings`）把数据 key 和模型 key **按请求注入**，
   `get_credential` 先读请求级凭据，容器不碰 Supabase。这会改 `agents/tool_context.py`，桌面端、命令行、MCP
   都会受影响，需要单独的 PR 并先补测试。
4. `search_stock_by_name` 只读一个市值缓存 JSON 文件和一个调试开关，内容不含用户数据，倾向放行。
5. `market_regime` 内部会用环境变量里的模型凭据再发起一次模型调用；共用容器里没有这些环境变量，需要确认它在缺凭据
   时是优雅跳过而不是报错。

## 未验证

- 条件分支（`has_cloud`）的真实行为，需要逐个工具读代码并补测试。
- `AgentRuntime` 并发工具批次用 `ThreadPoolExecutor.submit`（`cli/runtime.py`）而没有 `copy_context`；
  `tushare_client` 的运行期 token 是 `ContextVar`。各工具在池线程里自己调用 `ensure_tushare_token`，多半没问题，
  但改成请求级凭据时必须确认。
