# dsh-memlayer

Memlayer 的 DSH 适配器，版本 0.2.0-beta.2，随源码分发。桥接验收覆盖 Memlayer 0.3.1。

## 本机试用

先从 Memlayer 仓库根目录启动本机桥，再构建插件。

    python memlayer/bridge.py --root ./data/dsh-store --port 8765
    cd plugin
    npm ci --ignore-scripts
    npm run check
    npm pack --ignore-scripts

从 Release 下载插件包后，在独立配置安装。宿主验证版本为 DSH 0.2.2-rc.5。

    dsh plugin --profile <独立配置名> add <插件包路径>

建议为试用配置单独设置 DSH_HOME。配置补丁默认禁用。启用时将 enabled 设为 true，并在 projectAllowlist 填入项目根绝对路径（默认取 Git 根）或完整的 p_ 存储键。

| 配置 | 默认值 | 行为 |
|---|---|---|
| enabled | false | 显式启用 |
| mode | shadow | 记录到本机；active 额外启用召回，disabled 停用 |
| projectAllowlist | [] | 精确允许指定项目 |
| baseUrl | http://127.0.0.1:8765 | 本机 HTTP 源 |
| captureAssistant | false | 启用后也收集所选项目的助手消息 |
| requestTimeoutMs | 300 | 影子投递时限，超时保留 spool |
| maxRecallChars | 2400 | active 模式的注入字符预算 |
| killSwitchFile | 未设置 | 配置的文件出现后暂停注入 |

影子事件写入本机 shadow.jsonl，待投递项写入本机 spool。敏感模式屏蔽覆盖常见格式，把两个文件按聊天日志保管。影子投递产生事件，人工批准候选后才会产生可召回的记忆。active 模式会把符合条件的历史证据发给 DSH 当前配置的模型。设置 enabled 为 false 或移除 bundle 可暂停采集。

升级旧沙箱时需要明确设置 enabled 和项目范围。Windows 项目键沿用大小写不敏感处理。其他平台的大小写不同路径会分别派生键。
