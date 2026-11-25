# Memlayer

**让 Agent 在新会话继续执行你已确认的项目决定。** 你可以核对记忆原话、适用范围和来源，改口时经审批替换旧规则，也可以只在特定条件下追加例外。

[下载 v0.3.1 beta](https://github.com/chilayt/memlayer/releases/tag/v0.3.1) · [接入 MCP](docs/MCP.md) · [实测结果与方法](docs/EVALUATION.md) · [English](README.md)

公开发布版通过 134 项引擎检查、16 项 DSH 适配器检查和 MCP 端到端检查。后续开发快照也做了同模型的长程对照。九组配对首交盲审，Memlayer 通过 7/9，Memorix 通过 5/9。含恢复和返工的最终结果均为 9/9。测试版本、方法和结果见[评测报告](docs/EVALUATION.md)。单用户本机 beta 的研究笔记见 [memlayer-notes](https://github.com/chilayt/memlayer-notes)。

## 是什么

memlayer 给 LLM agent 提供跨会话长期记忆，核心特点：

- **Markdown 真相源**：事实存为可读、可 diff、可重建的 `.md` 台账；SQLite 仅作可丢弃索引。
- **人工审批门**：批准后候选才可召回。可确认新增、替换现行偏好，或只在指定条件增加例外。
- **可撤回**：墓碑 + 撤回账本 + 同源级联 + store 外镜像 + 启动重放，防备份回灌复活。
- **项目隔离**：调用方使用独立项目命名空间。DSH 插件从项目根目录派生存储键。
- **双时间效度**：valid_from / valid_to + recorded_at。
- **检索**：词项 + 语义混合（RRF 融合），语义层可开关、shadow 对比。

## 架构

| 组件 | 文件 | 说明 |
|---|---|---|
| 存储引擎 | `memlayer/memlayer.py` | 台账/索引/写队列/幂等/撤回/重放 |
| HTTP 桥 | `memlayer/bridge.py` | loopback-only，单写者，召回/撤回/候选 API |
| 语义层 | `memlayer/semantic.py` | 嵌入客户端 + 缓存 + RRF（可选，默认关） |
| DSH 插件 | `plugin/` | TypeScript，shadow 捕获 + active 召回注入 |
| 测试 | `tests/` | 存储、审批、撤回和重启回归 |
| 评测 | `eval/` | 合成中文检索题集与本机评测 |

详见 [docs/DESIGN.md](docs/DESIGN.md)。

## 快速开始

需要 Python 3.11 或以上。DSH 适配器另需 Node.js 22 或以上。

```bash
# 存储引擎自测（纯标准库）
python tests/test_phase1.py
python tests/forget-test.py

# 起桥（loopback）
python memlayer/bridge.py --root ./data/store --retraction-mirror ./data/mirror --port 8770
```

客户端可按 [HTTP API](docs/API.md) 接入。支持本地 MCP 的 Agent 可使用 [MCP 接入、审批和实测说明](docs/MCP.md)，其中包括 Codex CLI、OpenCode v2 和 Claude Code 的配置示例。DSH 用户可从 [适配器说明](plugin/README.md) 开始。

### 本机中文语义召回

准备 BGE-small-zh-v1.5 的 ONNX 模型和 tokenizer，在同一个目录放入 `model_quantized.onnx` 和 `tokenizer.json`。安装 CPU 版 `onnxruntime` 与 `tokenizers` 到运行桥的 Python，也可以用 `pip install --target <模型目录>/packages onnxruntime tokenizers` 安装到独立目录。[原始模型](https://huggingface.co/BAAI/bge-small-zh-v1.5) · [量化 ONNX](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)

起桥时加 `--local-embedding <模型目录>` 即可。运行 `python eval/eval-local-semantic.py <模型目录>` 验证。这个选项通过 CPU 在本机计算向量，默认部署仍可使用纯词法。

可选启用本机重排。将 BGE-reranker-v2-m3 的 INT8 ONNX 文件取名为 `reranker/model_quantized.onnx`，并将对应 tokenizer 放到 `reranker/tokenizer.json`。两个路径相对于本机向量模型目录。重启本机桥后，`/health` 的 `semantic.reranker` 会显示 `ready`。[原始模型](https://huggingface.co/BAAI/bge-reranker-v2-m3) · [INT8 ONNX](https://huggingface.co/onnx-community/bge-reranker-v2-m3-ONNX/tree/6f5ff65298512715a1e669753bc754d2bc8f367b)

运行 `python eval/eval-local-semantic.py <模型目录> --holdout --require-reranker` 可验证原回归样例和补充的改写与无关问题。

## 重要边界（务必读）

**撤回 ≠ 彻底遗忘。** 撤回停止召回，但不删除原文/日志/备份/已发上下文/外部副本。详见 [docs/SAFETY-BOUNDARIES.md](docs/SAFETY-BOUNDARIES.md)。

显式设置 MEMLAYER_SEMANTIC=1 和 MEMLAYER_EMB_KEY 后可启用外部嵌入。查询和候选事实文本会发给该服务，含 shadow 模式。本机桥只接受本机原生客户端，启动它的账户和机器上的可信进程共享这一服务边界。

## 许可

Apache-2.0（代码）。见 [LICENSE](LICENSE)。
