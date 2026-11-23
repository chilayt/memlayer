# 本机 HTTP API

启动 bridge.py 后，从同一台机器使用 Python、Node.js 或其他原生客户端调用下列接口。POST 的媒体类型为 application/json。选择固定的 project 和 subject，可将应用的记录分区。示例使用虚构项目 p_demo 和主题 author-note。

| 操作 | 端点 | 关键字段 |
|---|---|---|
| 探活 | GET /health | 返回桥版本、写队列和语义状态 |
| 读现行记忆 | GET /v1/facts | project、可选 subject、q、limit，默认返回已确认的现行事实 |
| 提交候选 | POST /v1/candidates | project、subject、text、source_event_id，来源为调用方保存的稳定事件编号 |
| 核对候选 | GET /v1/candidates | project、可选 status=pending |
| 确认新增 | POST /v1/candidates/approve | candidate_id、expected_hash，取自候选的 id 和 content_hash |
| 确认替换 | POST /v1/candidates/approve-replacement | 加 supersedes_fact_id，指向同一项目和主题的现行已确认事实 |
| 确认例外 | POST /v1/candidates/approve-exception | 同替换字段。候选正文写明条件和例外做法 |
| 拒绝 | POST /v1/candidates/reject | candidate_id、expected_hash、可选 note |
| 撤回 | POST /v1/facts/retract | fact_id、可选 reason。可同时提供 project 和 subject 约束范围 |

候选提交不会改变现行召回。批准时校验内容哈希，重复批准同一个决定返回相同事实 ID。发现版本变更时返回 409，重新读取后由作者核对。默认规则和已批准的适用例外在同一条现行记录中，保留双方来源。撤回影响见[边界说明](SAFETY-BOUNDARIES.md)。

桥只绑定本机回环地址。项目是应用层的数据范围，能访问该端口的本机进程可调用 API。上层应用负责保护用户权限、决定哪些内容进入模型，以及呈现审批和撤回操作。
