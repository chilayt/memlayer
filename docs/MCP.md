# 用 MCP 接入 Memlayer

Memlayer 0.3.0 beta 提供本机 stdio MCP。客户端在与记忆库同一台机器上启动适配器，适配器再连接 Memlayer 本机桥。每个配置绑定一个 `project` 和一个 `subject`。下面用独立的演示项目 `p_demo`，主题默认为 `author-note`。

## 安装和启动

在解压的源码目录打开终端，启动本机桥。首次试用可使用单独的数据目录。

```bash
python memlayer/bridge.py --root ./data/mcp-demo --port 8770
```

保持桥运行。在适配器所用的 Python 环境安装可选依赖。适配器需要 Python 3.11 或以上。

```bash
python -m pip install -r requirements-mcp.txt
```

以下示例中的 Python 和脚本路径都要换成实际的绝对路径。Windows 上可以使用 `C:/...` 路径格式。Python 必须是上一步安装了 MCP 依赖的那个解释器。

## Agent 配置示例

### Codex CLI

在 Codex 的 `config.toml` 中加入下列配置。可运行 `codex mcp list` 检查注册，再在新会话中请 Codex 调用 `recall_memory`。

```toml
[mcp_servers.memlayer]
command = "/absolute/path/to/python"
args = [
  "/absolute/path/to/memlayer/mcp_stdio.py",
  "--bridge", "http://127.0.0.1:8770",
  "--project", "p_demo",
  "serve",
]
```

[Codex MCP 配置文档](https://developers.openai.com/codex/mcp/)

### OpenCode v2

在项目根目录的 `opencode.json` 或 `opencode.jsonc` 加入以下配置。从该目录运行 `opencode mcp list` 检查连接状态。首次启动若显示未配置 MCP，稍等几秒后再查一次。

```json
{
  "mcp": {
    "servers": {
      "memlayer": {
        "type": "local",
        "command": [
          "/absolute/path/to/python",
          "/absolute/path/to/memlayer/mcp_stdio.py",
          "--bridge", "http://127.0.0.1:8770",
          "--project", "p_demo",
          "serve"
        ]
      }
    }
  }
}
```

[OpenCode 本机 MCP 配置文档](https://opencode.ai/docs/mcp-servers/#local)

### Claude Code

下面的命令把服务注册到当前项目的本地配置。用 `claude mcp list` 检查注册状态。

```bash
claude mcp add --transport stdio --scope local memlayer -- "/absolute/path/to/python" "/absolute/path/to/memlayer/mcp_stdio.py" --bridge http://127.0.0.1:8770 --project p_demo serve
```

[Claude Code 本机 MCP 配置文档](https://code.claude.com/docs/en/mcp#option-3-add-a-local-stdio-server)

### 其他本地客户端

选择本地 stdio 服务，以安装了依赖的 Python 作为可执行命令。传入脚本路径、`--bridge`、`--project` 和 `serve`。使用 `mcpServers` 结构的客户端可用如下配置。

```json
{
  "mcpServers": {
    "memlayer": {
      "command": "/absolute/path/to/python",
      "args": [
        "/absolute/path/to/memlayer/mcp_stdio.py",
        "--bridge", "http://127.0.0.1:8770",
        "--project", "p_demo",
        "serve"
      ]
    }
  }
}
```

| 工具 | 作用 |
|---|---|
| `recall_memory` | 按问题检索此项目与主题中已确认且现行的记忆，返回原文、记忆编号和来源编号 |
| `propose_memory` | 把作者明确表达的长期偏好提交为待审候选，返回候选编号和待审状态 |

客户端可以给提案传入真实消息编号 `source_ref`。省略时，适配器从文本生成稳定的键，网络重试会返回同一个候选。要在以后重新提交完全相同的文本，请提供新的真实来源编号。

可在 Agent 的项目指令中约定：当用户用「以后都这样」「以这个为准」或明确改口来表达长期偏好时，调用 `propose_memory`，然后把候选编号交给用户审阅。一次性要求只用于当前任务。

## 由你确认候选

在源码目录的交互终端运行 `pending`，核对列表中的项目、主题、候选原文和来源编号。

```bash
python memlayer/mcp_stdio.py --bridge http://127.0.0.1:8770 --project p_demo pending
```

用实际候选编号替换示例中的 `c_0123456789abcdef`，运行 `approve`。若 Agent 配置里指定了 `--subject`，这里也传入相同值。

```bash
python memlayer/mcp_stdio.py --bridge http://127.0.0.1:8770 --project p_demo approve c_0123456789abcdef
```

终端再次显示候选原文和范围。确认无误后，按提示输入 `APPROVE c_0123456789abcdef`。输入其他内容或退出会保留待审状态。批准成功后，记忆才能被 `recall_memory` 检索。MCP 工具集中只有查询和提案，审批命令在独立终端执行。

这里的 `approve` 用于新增记忆。若你要替换旧记忆，需在支持替换审批的宿主中选定旧记忆，或由操作者使用[候选替换 API](API.md)。

## 实测结果

我们用 **Codex CLI 0.163.0** 连接隔离库，以虚构数据运行了两个真实 Agent 会话。

| 步骤 | 观察到的结果 |
|---|---|
| 查询已批准的灯塔账簿偏好 | Codex 从 MCP 读到已批准的原文和来源 |
| 提交巡夜账簿的新偏好 | 工具返回候选编号和 `pending` 状态 |
| 审批前再查一次新偏好 | 返回零条匹配记忆 |
| 模拟操作者在独立终端确认 | 审批流程显示原文，生成已确认事实 |
| 开一个全新的 Codex 会话再查 | MCP 返回同一条偏好和原来源编号 |

另用 Windows 上的 **OpenCode 2.2.7** 验证了文档中的项目配置。等待客户端启动后，`opencode mcp list` 显示 `memlayer connected`。上表的候选审批和召回流程使用 Codex CLI 执行。

安装可选依赖后，可运行 `python tests/mcp-stdio-check.py` 复现 stdio 握手、项目和主题隔离、无效输入、重复提交、交互审批和撤回流程。上面的其他客户端配置参照各宿主的本机 stdio 接入格式。

桥运行在回环地址。能直接访问该桥的本机进程处在同一信任边界。向 Agent 提供记忆前，请选定合适的项目与主题。详见 [HTTP API](API.md) 和[边界说明](SAFETY-BOUNDARIES.md)。
