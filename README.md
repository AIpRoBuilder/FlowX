# FlowX MCP Server

FlowX is an MCP server for creating workflows through conversation, designed to integrate easily with agent systems such as Hermes, WorkBuddy, and TraeWork.

An [MCP](https://modelcontextprotocol.io) server that exposes the **`meta_agent`** workflow
builder and the **`ag_ui_workflow`** runtime engine as a set of tools, so any MCP client
(Claude Code, Cursor, Codex, VS Code Copilot Chat, ...) can:

1. **create** a new AG-UI workflow from a natural-language requirement,
2. **dynamically update** a workflow's backend artifacts (`workflow.json`, `{node}.py`,
   `main.py`) from a change prompt,
3. **start** the generated FastAPI backend engine for a workflow,
4. **reload** the backend after an update,
5. **inspect** the input format required by every user-input node,
6. **run** a workflow step from a chat message and collect the results.

It follows the same `FastMCP` tool-registration pattern used by the `hermes-agent`
MCP server. By default it serves over stdio, and it can also serve over
Streamable HTTP when you want clients to attach to an already-running instance.

---

## Architecture

```
MCP client  ──stdio / streamable-http──►  flowx_mcp.server (FastMCP)
                │
                ├── meta_agent.AgentBuilder  ──►  LLM (DeepSeek/...)
                │       (create / update / plan / generate)
                │
                └── subprocess: python main.py  ──►  FastAPI backend
                  │                                  │
                  │   POST /api/run-step (SSE)       │
                  └──────────────────────────────────┘
               ag_ui_workflow.WorkflowEngine
```

The MCP server keeps a per-workflow `WorkflowHandle` (an `AgentBuilder` plus the running
backend process, port and session state). Tools 3–6 talk to the running backend over HTTP
(`urllib`, no extra deps) and parse the AG-UI SSE event stream.

## Provided tools

| Tool | Purpose |
| --- | --- |
| `create_workflow` | Build a workflow from a requirement into `workspace/workflow_name`. |
| `update_workflow_node` | Amend `workflow.json` + `{node}.py` + `main.py` from a change prompt, then invalidate the current runtime session/backend so stale processes cannot keep serving the old graph. |
| `start_backend` | Launch the generated FastAPI backend for a workflow. |
| `reload_workflow` | Restart the backend (picks up updated node files / `workflow.json`). |
| `restart_builder` | Recreate the in-memory `AgentBuilder` for a workflow from disk and optionally restart the backend. |
| `get_node_input_formats` | List every user-input node and the input it expects. |
| `run_workflow_step` | Format a chat message into a step input, run it, return results. |
| `list_workflows` | (helper) List workflows known to the server. |
| `list_workflow_folders` | List workflow folders discovered on disk under the workspace root. |
| `upload_workspace_input_file` | Save a base64-encoded file into `workspace/inputs` and return its path. |
| `list_workflow_python_files` | List all `.py` files under a workflow folder. |
| `get_workflow_json` | Read the root `workflow.json` file for a workflow folder and return it as JSON. |
| `get_workflow_files` | Read specific workflow files by file name or relative path. |
| `get_workflow_binary_files` | Read specific workflow binary files and return base64 content plus MIME type. |
| `replace_workflow_files` | Replace specific workflow files by file name or relative path. |

## Setup

`meta_agent` and `ag_ui_workflow` must be importable. Install them editable:

```bash
python3.10 -m pip install -e /Users/user/Desktop/codes/meta_agent --no-deps
python3.10 -m pip install -e /Users/user/Desktop/codes/ag_ui_worflow --no-deps
python3.10 -m pip install -r requirements.txt
```

If you do not want to `pip install -e` them, set `FLOWX_EXTRA_PATHS` to a colon-separated
list of their source roots and the entry point will add them to `sys.path`.

Copy `.env.example` to `.env` and fill in your LLM key.

## Run

```bash
# default: stdio transport for local MCP hosts
python3.10 run_server.py
# or with verbose logging
python3.10 run_server.py --verbose

# streamable HTTP transport for a long-running remote server
python3.10 run_server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

Use stdio when the MCP host should launch FlowX itself. Use Streamable HTTP when
you want to keep one FlowX instance running and let clients attach to it by URL.

## Local stdio MCP client config (e.g. Claude Desktop / VS Code)

```jsonc
{
  "mcpServers": {
    "flowx": {
      "command": "python3.10",
      "args": ["/Users/user/Desktop/codes/FlowX/run_server.py"],
      "env": {
        "FLOWX_LLM_PROVIDER": "deepseek",
        "FLOWX_LLM_MODEL": "deepseek-chat",
        "FLOWX_LLM_API_KEY": "<your key>",
        "FLOWX_DEFAULT_WORKSPACE": "/Users/user/Desktop/codes/flowx_workspaces"
      }
    }
  }
}
```

## Remote MCP client config for an existing FlowX server

If FlowX is already running on the remote host, start it there with the HTTP
transport and connect the client to the MCP endpoint instead of spawning a fresh
process over SSH.

Start the server on the remote machine:

```bash
cd /home/testuser/FlowX
export FLOWX_LLM_PROVIDER=deepseek
export FLOWX_LLM_MODEL=deepseek-chat
export FLOWX_LLM_API_KEY='<your key>'
export FLOWX_DEFAULT_WORKSPACE=/home/testuser/flowx_workspaces
export FLOWX_EXTRA_PATHS=/home/testuser/meta_agent:/home/testuser/ag_ui_worflow
python3 run_server.py --transport streamable-http --host 0.0.0.0 --port 8000 --verbose
```

Then point a URL-capable MCP host at that running server:

```jsonc
{
  "mcpServers": {
    "flowx-remote": {
      "url": "https://{url}/mcp"
    }
  }
}
```

If your MCP host only supports stdio launch commands, keep using the SSH pattern
below.

## Example workflow prompts

The following prompts are ready to paste into an MCP client that is already
connected to `flowx-remote`.

### 1. `stock_pressure`

<details>
<summary>Prompt</summary>

```text
使用 flowx-remote 创建一个名字是 stock_pressure 的工作流。

根据下述理论构建一个工作流，根据最近 n 个交易日（用户指定）计算出指定股票（用户指定）庄家未来可能出货的价格位置，并在图表上显示。

庄家出货压力峰预测理论模型

一、模型目标

根据历史成交数据、筹码分布、庄家成本和历史高点信息，寻找未来可能形成出货压力的价格区域。

核心假设：
1. 庄家在股价大跌后低位吸筹。
2. 吸筹阶段形成庄家平均成本区。
3. 后续拉升过程中，庄家通过试盘测试上方卖压。
4. 历史大量成交区域和前期高点会形成动态压力。
5. 出货压力点由多因素共同决定。

二、输入数据

每日行情：
Date: 日期
Open: 开盘价
High: 最高价
Low: 最低价
Close: 收盘价
Volume: 成交量

三、筹码分布模型

将价格区间划分为 N 个价格桶。

对于每个价格区间 p：
C(p)=Σ Volume(t)

其中：
C(p) 表示该价格区域累计成交筹码数量。

成交峰值：
P_peak = argmax C(p)

表示市场成本最密集区域。

四、庄家吸筹成本估计

选择吸筹阶段：

条件：
1. 股价经历大幅下跌。
2. 成交量明显放大。
3. 股价进入横盘震荡。

庄家平均成本：
C_dealer = Σ(P_t × V_t) / ΣV_t

其中：
P_t: 成交价格
V_t: 成交量

得到庄家主要持仓成本。

五、历史压力峰计算

定义压力评分：
Pressure(p)= w1C(p) + w2High_Test(p) + w3Profit(p) + w4Gain(p)

其中：
1. 成交密集压力 C(p)
表示该价格区域历史交易筹码数量。

2. 历史高点压力 High_Test(p)
统计：
- 历史是否出现过高点
- 是否多次冲高失败
- 是否形成顶部区域

3. 获利盘压力 Profit(p)
计算：
Profit(p)=ΣC(x), x<p

表示当前价格以下有多少筹码处于盈利状态。

盈利筹码越多：
潜在卖压越大。

4. 庄家收益压力 Gain(p)
Gain(p)= (p-C_dealer)/C_dealer

表示庄家在该价格位置的理论收益。

六、压力峰搜索算法

步骤1：
建立价格-成交量矩阵：
price_bins = divide(min_price,max_price,N)

步骤2：
统计：
volume_profile[p]

步骤3：
计算每个价格位置：
Pressure Score

步骤4：
排序：
Score 最高的位置作为第一压力峰。

输出：
Pressure_1
Pressure_2
Pressure_3

分别代表：
第一压力区
第二压力区
第三压力区

七、动态试盘验证

庄家拉升过程中观察：

情况 A：
价格上涨，成交量下降。
说明：上方阻力较小。

情况 B：
价格上涨，成交量突然放大，同时出现长上影线，随后回落。
说明：该价格区域存在大量卖盘。

定义：
P_pressure = 当前测试失败价格

八、综合出货价格模型

最终预测：
P_exit = argmax Pressure(p)

即：
最大压力评分对应价格。

实际计算：
P_exit ≈ 庄家成本 × (1+r)

并且：
接近历史成交峰。

其中：
r: 庄家目标收益率。

九、Python 实现思路

输入：
K 线数据 DataFrame

计算：
1. 计算成交量分布
2. 估计庄家成本
3. 寻找历史高点
4. 计算盈利筹码比例
5. 计算 Pressure Score
6. 输出压力峰

伪代码：
for price in price_bins:
    score = (
        w1 * volume_density(price)
        + w2 * historical_high(price)
        + w3 * profit_ratio(price)
        + w4 * dealer_gain(price)
    )

rank(score)

return top_pressure_prices

十、最终解释

庄家未来可能出货的位置，不是简单的历史最高价。
```

</details>

<p align="center">
  <img src="assets/stock_pressure.png" alt="stock_pressure workflow example" width="720" />
</p>

### 2. `stock_distribution`

<details>
<summary>Prompt</summary>

```text
使用 flowx-remote 创建一个名字是 stock_distribution 的工作流。

根据如下理论构建一个可以预估当前和历史价格大小户筹码分布并画成图的工作流。

## 角色
你是一名量化研究员，负责根据 1 小时级 OHLCV 数据建立“隐含大小户筹码状态”模型，并识别吸筹、派发与潜在价格压力峰。

## 重要原则
1. 只有价格和成交量时，无法直接观测真实账户的大户/小户持仓。
2. large_ratio / small_ratio 是模型隐变量估计，不是真实账户持仓。
3. “上涨=大户卖给小户、下跌=小户卖给大户”是待检验假设，而不是事实。
4. 所有实时特征只能使用当前及过去数据，禁止未来函数。
5. 输出概率/置信度，不把模型结果描述成确定事实。

## 输入
CSV 至少包含：
datetime, open, high, low, close, volume

## 核心状态
H_t：估计大户筹码比例
L_t：估计小户筹码比例
H_t + L_t = 1
D_t = H_t - L_t = 2H_t - 1

R_t = (P_t-P_{t-1})/P_{t-1}
V*_t = V_t / EMA(V_t)

## 状态转移
最简可校准模型：

ΔH_t = -alpha * tanh(R_t / sigma_R) * V*_t

并限制 H_t ∈ [h_min, h_max]。

解释：
- 放量上涨：模型倾向 H 下降，即“大户→小户”的隐含分散；
- 放量下跌：模型倾向 H 上升，即“小户→大户”的隐含聚合；
- 小波动/正常成交：状态变化较小。
```

</details>

<p align="center">
  <img src="assets/stock_chip_distribution.png" alt="stock_distribution workflow example" width="720" />
</p>

### 3. `picture_to_svg`

<details>
<summary>Prompt</summary>

```text
使用 flowx-remote 创建一个名字是 picture_to_svg 的工作流。

创建一个上传微信二维码图片、自动去除人名信息并转成 SVG 格式的工作流。
```

</details>

<p align="center">
  <img src="assets/picture_to_svg.png" alt="picture_to_svg workflow example" width="720" />
</p>

### 4. `policy_scrawler`

<details>
<summary>Prompt</summary>

```text
使用 flowx-remote 创建一个名字是 policy_scrawler 的工作流。

要求：
1. 从全国政策发布机构名录获取需要的机构名称模版。
2. 结合全国省-市-县名称，整理全国政策机构的具体搜索名称。
3. 使用 Playwright 模拟浏览器，在百度搜索对应机构官网名称。
4. 构建一个可以爬取全国政策机构官网 URL 的工作流。
```

</details>

<p align="center">
  <img src="assets/policy_scrawler.png" alt="policy_scrawler workflow example" width="720" />
</p>

### 5. `start-up-hiring`

<details>
<summary>Prompt</summary>

```text
flowx-remote-us帮我构建一个到LinkedIn上找n个正在招人的AI startups的创始人的爬虫工作流
```

</details>

<p align="center">
  <img src="assets/start-up-hiring.png" alt="start-up-hiring workflow example" width="720" />
</p>

## Contact

If you are interested in using FlowX or co-building it, contact me at [peterxcx@gmail.com](mailto:peterxcx@gmail.com).

WeChat QR code:

<img src="assets/qrcode.svg" alt="WeChat QR code" width="220" />
