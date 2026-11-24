# 用 MCP 接入 Memlayer

使用支持本地 stdio MCP 的客户端，在与记忆库同一台机器上启动适配器。适配器连接现有 Memlayer 本机桥。一个配置只访问一个 `project` 和一个 `subject`，由你在启动参数里选择。

## 启动

在 Memlayer 源码目录启动桥，并在适配器使用的 Python 环境安装可选依赖。

```bash
python memlayer/bridge.py --root ./data/store --port 8770
```

```bash
python -m pip install -r requirements-mcp.txt
```

在 MCP 客户端配置本地命令。以下是常见的 `mcpServers` 格式。将命令和脚本换成你的绝对路径。示例用虚构项目 `p_demo`，默认主题是 `author-note`。

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
| `recall_memory` | 根据问题查询此范围内已确认且现行的记忆，返回内容和来源编号 |
| `propose_memory` | 将作者明确表达的长期偏好提交为待审候选，返回候选编号 |

## 人工核对

候选可以写入待审台账。只有批准后才会成为可被检索的记忆。MCP 暴露的操作限于查询和提案，审批在独立终端进行。

```bash
python memlayer/mcp_stdio.py --project p_demo pending
python memlayer/mcp_stdio.py --project p_demo approve <候选编号>
```

终端会显示候选原文和范围。核对后，在交互终端按提示输入 `APPROVE <候选编号>`。省略确认或输入其他内容会保留待审状态。如客户端配置了 `--bridge` 或 `--subject`，请在核对命令里使用相同参数。

`source_ref` 可由提供真实消息编号的客户端传入。省略时会按相同文本生成稳定的提案键，避免网络重试重复提交。要在之后重新提议同一句话，可以提供新的真实来源编号。

安装可选依赖后，可运行 `python tests/mcp-stdio-check.py` 验证真实 stdio 握手、查询范围、待审隔离和审批流程。

适配器走本机 stdio 和回环 HTTP。能直接访问桥的本机进程处在同一信任边界。向 Agent 公开确认过的记忆前，请选择合适的项目和主题。更多信息见 [HTTP API](API.md) 与[边界说明](SAFETY-BOUNDARIES.md)。
