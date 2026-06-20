# Crypto Strategy Lab

一个面向 crypto 的统计 + 事件驱动交易策略软件 MVP。当前版本专注于研究和回测，不做真实资金下单。

## 已包含

- 事件驱动回测引擎：行情 bar、外部事件、信号、订单、成交分层处理
- 可配置策略模板：事件策略、均值回归、趋势跟随、通道突破、RSI、Bollinger + RSI、固定权重持有
- 行情数据源接口：Binance、OKX、Bybit、Coinbase、KuCoin、Kraken、Bitget、CoinGecko 的公开 K 线/OHLC
- 量化研究工具箱：交易对池、SMA/EMA/RSI/Bollinger/ATR/MACD 指标分析、回测 JSON/CSV 导出
- 用户策略配置：每个用户可以保存多个策略参数组合，设默认配置，并选择配置运行回测
- 回测可信度设置：手续费、滑点、成交延迟、资金费率、爆仓/保证金、最大持仓、止损止盈、样本外测试、参数网格优化
- 监控和交易记录：回测运行历史、回测报告详情、订单状态、成交记录、审计日志、名义成交额、手续费、资金费、强制退出和异常运行概览
- Paper trading：模拟账户、下单前预检、市价/限价/Post-only/IOC/FOK/条件单参数、手动买卖、开多/平多、按策略自动执行、持仓记录
- 组合和风控：现金、持仓、手续费、滑点、Paper/Live 开关、单笔订单限制、最大开仓数量、最大杠杆、连续亏损停机、单币权重、总风险敞口、spot long-only 限制
- CLI：用 CSV 行情和事件数据跑回测
- 单元测试：覆盖 feed 排序、风控和完整回测路径

## 快速开始

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
PYTHONPATH=src python3 -m crypto_strategy_lab.cli backtest \
  --bars data/sample_bars.csv \
  --events data/sample_events.csv \
  --cash 10000
```

启动多人 Web 工作台：

```bash
python3 -m pip install -e .
CRYPTO_LAB_SECRET_KEY="replace-with-a-long-random-secret" \
CRYPTO_LAB_ADMIN_EMAIL="admin@example.com" \
CRYPTO_LAB_ADMIN_PASSWORD="change-me-now" \
crypto-lab-web --host 127.0.0.1 --port 8092
```

打开 `http://127.0.0.1:8092` 登录。默认角色权限：

| 角色 | 权限 |
| --- | --- |
| `viewer` | 查看工作台、策略、监控和自己的交易记录 |
| `analyst` | 运行回测，管理自己的策略配置，查看自己的交易记录 |
| `trader` | 运行回测，管理自己的策略配置和交易所 API，执行 paper trading，查看自己的交易记录 |
| `admin` | 用户管理，查看所有 API/策略配置、所有交易记录和系统监控，拥有 live trading 权限开关 |

角色只是默认权限模板。管理员可以在配置页的“用户与权限”里对单个用户做权限覆盖：勾选额外授权，或取消某个角色默认权限。前端页面会按最终权限动态显示：

- `状态`：监控、Paper 账户和持仓。
- `交易记录`：手动/Paper 交易和成交记录。
- `配置`：行情数据源、策略配置、回测、API、用户管理。

后端接口也按最终权限校验，不只依赖角色名称。系统会阻止管理员禁用自己，或撤销自己当前的用户管理权限，避免误操作锁定账号。

API Key 会使用 `CRYPTO_LAB_SECRET_KEY` 派生出的密钥加密后存入 SQLite。部署给其他人使用时必须更换默认管理员密码，使用 HTTPS，并把 `CRYPTO_LAB_SECRET_KEY` 放到服务器密钥管理或环境变量中。

手机临时访问可以用 quick tunnel：

```bash
./scripts/start_public_demo.sh
```

终端会打印一个 `https://*.trycloudflare.com` 地址。这个地址是临时的，长期使用请看 [docs/deployment.md](docs/deployment.md)。

## 行情数据源

配置页的“行情数据源”可以直接拉取公开 K 线并保存为本地 CSV，成功后会自动填入回测的“行情 CSV”。

当前内置公共数据源：

| 数据源 | 市场 | Symbol 示例 | 说明 |
| --- | --- | --- | --- |
| Binance Spot | 现货 | `BTCUSDT` | `/api/v3/klines` |
| Binance USD-M Futures | U 本位合约 | `BTCUSDT` | `/fapi/v1/klines` |
| OKX | 现货/Swap/合约 | `BTC-USDT`, `BTC-USDT-SWAP` | `/api/v5/market/history-candles` |
| Bybit Spot | 现货 | `BTCUSDT` | `/v5/market/kline` |
| Bybit Linear | 线性合约 | `BTCUSDT` | `/v5/market/kline` |
| Coinbase Exchange | 现货 | `BTC-USDT` | `/products/{product_id}/candles` |
| KuCoin Spot | 现货 | `BTC-USDT` | `/api/v1/market/candles` |
| Kraken | 现货 | `XBTUSD` | `/0/public/OHLC` |
| Bitget USDT Futures | USDT 合约 | `BTCUSDT` | `/api/v2/mix/market/history-candles` |
| CoinGecko OHLC | 聚合价格 | `bitcoin` | 免费 OHLC 不含成交量，volume 写 0 |

所有数据源第一版都是单次 REST 拉取，不做长时间分页回补；大样本历史数据后续应增加分页、限速、失败重试和任务队列。

## 量化工具箱

配置页新增了主流量化工具常见的研究辅助功能：

- `交易对池`：保存一组 symbol/universe，可设默认，快速填入行情拉取、指标分析和 paper 交易。
- `指标分析`：读取当前行情 CSV，计算 SMA、EMA、RSI、Bollinger Bands、ATR、MACD，并展示最新值和最近序列。
- `报告导出`：回测报告详情支持导出完整 JSON 或成交 CSV，便于二次分析和分享。

这些功能参考了 LEAN/Freqtrade/Backtrader/vectorbt 常见工作流：资产池/市场选择、技术指标、回测报告、导出和进一步分析。

## 策略定制

Web 工作台支持按用户保存策略配置。当前内置模板：

| 策略 | 适用 | 可调参数 |
| --- | --- | --- |
| 事件 + Z-score 均值回归 | 山寨币现货、低杠杆 long-only 合约 | lookback、entry/exit z-score、基础仓位、事件仓位、事件阈值、事件有效 bar 数 |
| 事件分数策略 | 公告、链上、情绪事件驱动 | 正负事件阈值、目标仓位、事件有效 bar 数 |
| 均线交叉趋势 | 趋势行情、动量山寨币 | 短均线、长均线、目标仓位、信号缓冲 |
| 通道突破 | 强趋势、突破型行情 | 突破周期、退出周期、目标仓位 |
| RSI 均值回归 | 震荡行情、急跌反弹 | RSI 周期、超卖阈值、退出阈值、目标仓位 |
| Bollinger + RSI 反转 | 波动回归、下轨反弹 | 布林周期、标准差倍数、RSI 参数、目标仓位 |
| 固定权重持有 | 基准组合、长期持仓模板 | 目标仓位 |

回测可以选择“当前表单参数”，也可以选择用户已保存的策略配置。管理员可以查看所有用户的策略配置；普通用户只能管理自己的配置。

## 回测可信度

配置页的“事件驱动回测”支持以下真实执行假设：

- 成本：手续费 bps、滑点 bps。
- 成交：按 bar 延迟执行信号，延迟成交会使用后续 bar 的价格和时间。
- 合约：perpetual 模式可设置资金费率、杠杆和维持保证金率，并按 bar 内 low 近似触发多头爆仓。
- 风控退出：止损、止盈、最大持仓 bar 数会生成强制平仓成交。
- 仓位限制：单笔最大名义额、单币最大权重、总敞口上限。
- 验证：可设置样本外比例，同一次回测会返回全样本、样本内、样本外指标。
- 参数优化：参数网格 JSON 会批量测试组合，并按收益率返回排名；网格结果不写入运行历史，避免污染交易记录。

当前实现是研究级 bar 回测：止损/止盈/爆仓使用 OHLC 的 high/low 近似，资金费率按固定 bps/bar 计算。要接近交易所实盘，还需要逐笔成交、订单簿深度、真实资金费时间表、交易所保证金阶梯、限价/市价单失败率和更严格的 walk-forward 验证。

## 监控和交易记录

每次 Web 回测都会持久化两类数据：

- `trade_runs`：一次策略运行，包含用户、策略、运行模式、参数、绩效指标、开始/结束时间。
- `trade_orders`：一次委托，包含 client order id、exchange order id、状态、请求价格、成交数量、均价、拒绝原因和原始响应摘要。
- `trade_records`：每笔成交，包含用户、策略、symbol、side、quantity、price、notional、fee、reason 和成交时间。
- `audit_logs`：关键操作审计，包含登录、改密码、用户/API Key 变更、风控更新、交易预检、下单和取消订单。

记录来源包括 `backtest` 和 `paper`。配置页的“已保存回测结果”会按 `run_mode=backtest` 单独展示回测历史，并可点“查看报告”查看该次回测的参数、核心指标和成交明细。

## Paper 自动交易

当前支持 paper 模式下的自动买卖和开平仓：

- 手动开多/买入：写入 `paper_positions`，扣减 paper cash。
- 手动平多/卖出：减少持仓，增加 cash，计算已实现 PnL。
- 订单类型：支持市价单、限价单、Post-only、IOC/FOK、止损、止盈、追踪止损、Reduce-only 参数；非立即成交的限价/条件单会保留在订单状态表，不会写成交记录。
- 按策略自动执行：选择已保存策略配置，使用当前 CSV 行情/事件源生成信号并执行为 paper 成交。
- 下单前预检：检查 Paper/Live 开关、单笔名义额、已实现亏损、连续亏损、最大开仓数量、最大杠杆、paper cash、可平持仓、禁止反向/重复开仓、Reduce-only、单币权重和总敞口。
- 所有 paper 成交都会写入 `trade_orders`、`trade_runs` 和 `trade_records`，并出现在监控和交易记录中。
- 配置页提供交易风控开关，可暂停 Paper 交易、限制单笔名义额、设置单币权重和总敞口上限，并运行 Paper/local 仓位、余额、未成交订单对账。

真实 live 下单端点默认关闭。除非设置 `CRYPTO_LAB_ENABLE_LIVE_TRADING=1`，并且配置页开启实盘交易开关，否则 `/api/live-trading/order` 不会进入真实交易路径。即使打开开关，当前版本也不会直接连交易所；还需要实现交易所私有 connector、真实订单回报、私有余额/持仓同步和密钥隔离后才能实盘。

如果想安装为命令：

```bash
python3 -m pip install -e .
crypto-lab backtest --bars data/sample_bars.csv --events data/sample_events.csv
```

## CSV 格式

行情文件：

```csv
timestamp,symbol,open,high,low,close,volume
2026-01-01T00:00:00+00:00,BTCUSDT,100,102,99,101,1200
```

事件文件：

```csv
timestamp,symbol,event_type,score,description
2026-01-03T12:00:00+00:00,BTCUSDT,news,0.75,positive ETF flow
```

`score` 建议使用 `[-1, 1]`，正数表示偏多，负数表示偏空。

## 项目结构

```text
src/crypto_strategy_lab/
  broker.py      # 模拟撮合、手续费、滑点
  cli.py         # 命令行入口
  data.py        # CSV 行情和事件源
  engine.py      # 事件驱动回测主循环
  models.py      # Bar / Signal / Order / Fill 等核心模型
  portfolio.py   # 现金、持仓、权益曲线
  reporting.py   # 回测指标
  risk.py        # 风控
  strategy.py    # 策略接口和示例策略
  strategy_registry.py # 策略模板、参数校验和工厂
  web.py         # FastAPI Web 工作台
  permissions.py # 角色权限
  security.py    # 密码哈希、会话、密钥加密
  store.py       # SQLite 用户、会话、API 设置、策略配置、订单、交易记录、审计日志、paper 持仓
  static/        # 前端页面
```

## 下一步建议

1. 接入交易所只读行情：Binance/Coinbase/OKX 的 websocket 和 REST。
2. 建立事件管道：新闻、公告、链上巨鲸、社交媒体情绪、宏观日历。
3. 加研究层：pandas/DuckDB 因子计算、walk-forward validation、参数稳定性分析。
4. 加执行层：在当前订单状态机上继续接真实交易所 connector；真实下单前必须有限价单、断路器、仓位上限、密钥隔离和审计日志。
5. 加租户隔离、审计日志、API Key 权限检查、2FA、密码重置和备份恢复。

这不是投资建议，当前代码仅用于策略研究和软件原型。
