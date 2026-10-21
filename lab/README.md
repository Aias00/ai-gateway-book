# 可运行的 HTTP 网关实验

需要 Python 3.10+，只用标准库，不需要真实模型、API key、外网或第三方 Python 包。从仓库根目录运行：

```sh
python3 -m lab.demo --evidence lab/evidence/demo.json
python3 -m unittest discover -s tests -v
```

第一条启动真正的环回 HTTP 模型替身、网关与 SSE 客户端，执行受授权切换、断流不重放、取消、并发、慢消费者和日志隐私实验，然后退出。第二条运行集成测试。`lab/evidence/demo.json` 是本机运行记录；时间戳、取消前已生成的字节数和队列阻塞次数可能随调度变化，不是性能基准。

交互模式：

```sh
python3 -m lab.demo --serve --port 8088
```

另开终端，用公开的实验凭据请求文本替身：

```sh
curl -N http://127.0.0.1:8088/v1/generate \
  -H 'Authorization: Bearer tenant-a-secret' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"first","prompt":"hi","capabilities":["text"],"max_output_units":100}'
```

这是教学协议，不兼容任何供应商 API。能力标签只检验路由匹配，不证明替身具备多模态或真实推理能力。重复相同租户的 `request_id` 返回 409；此字段只用于当前进程去重与关联，不提供结果重取、持久幂等或跨实例保证。

## 接口与预算

`POST /v1/generate` 仅接受 `request_id`、`prompt`、`capabilities`、`max_output_units`、可选布尔 `allow_failover`。未知字段拒绝，包括客户端提供的 `tenant`、`route` 和 `usage`。Bearer key 由服务端配置映射租户，不从模型参数读取租户身份。

`request_id` 为 1–64 个 ASCII 字母、数字、`_` 或 `-`；输入 UTF-8 最多 32,768 字节，输出上限默认最多 8 MiB。请求 JSON 最多 65,536 字节；这是独立的封装大小限制，不能只按解码后的 prompt 大小判断请求是否可接纳。

本实验的 `utf8-bytes-v1` 合同定义 **1 credit = 1 个 UTF-8 字节**：

- 预留：`len(prompt.encode("utf-8")) + max_output_units`。
- 完整用量：输入 prompt 字节数 + 所有 `token.text` 的 UTF-8 字节数。
- JSON、SSE 字段名及 HTTP 封装不计入这个教学单位。

这不是任何真实模型 tokenizer 或收费规则。多字节中文要按 UTF-8 字节计算，不能用 Python 字符数代替。替身凭配置凭据接收请求；网关只接受所选可信上游的独立 `usage` 事件，并核对合同、整数类型、输入和输出计数。模型正文里的 `usage=0` 只是数据。**完整 `usage` 加 `done`** 且下游写入完成后才结算；用量不合法、缺失、断流或取消都保守标为 `unknown` 并保留整笔预留。

`Gateway.ledger.snapshot(tenant)` 是进程内观测，包含 `budget_units/spent_units/held_units/available_units/active`。预留、并发接纳和余额检查在同一把锁内。完成后释放并发名额；未知用量保留预算，直至有权操作者取得可信凭证调用进程内 `Ledger.reconcile(tenant, request_id, actual_units)`。客户端没有对账或豁免债务的 HTTP 接口。

上述未知规则针对已经可能开始执行的请求；可以证明请求尚未发送，或收到受信任未开始拒绝时，释放预留。

## 流式协议与切换边界

CR 到达便立即作为行边界处理，不等待下一字节。为确保分块后到达的 LF 不使大小计量漏算，CR 行先保守计入潜在 CRLF 的两个结束字节；CR-only 的临界大帧可能比实际线上字节数略早达到限制，但不会突破 32 KiB 线上上界。

正常事件为 `token`（`{"text":"..."}`）→ `usage`（合同和 input/output_units）→ `done`（`{}`）。HTTP/1.1 chunked 传输由客户端解码后，再按完整 SSE 帧解析；每帧最多 32 KiB。支持 LF、CRLF、CR 行结束、跨 HTTP 分块的 UTF-8、注释和多 data 行的换行拼接；EOF 前没有空行结束的帧不会派发。应用层要求 event/data 与 JSON 对象，不支持 id/retry、自动重连或无 event 的默认 message。这是有明确边界的 SSE 子集。

默认 `allow_failover=false`，不自动重放 POST。只有请求明确设置 `true`，并且服务端对该上游配置 `Route(..., trust_not_started=True)`，上游返回 **HTTP 503 + `X-Lab-Execution: not-started`**，才承认这个私有协议的“没有执行也没有计费”承诺。在首次客户端响应前，最多尝试 `Settings.max_attempts=2` 个能力匹配上游。实验连连接失败也不自动切换，以保持唯一且可审查的切换入口。

普通 503、无输出就断开、发送后超时、HTTP 200 后无完整首帧、已输出后断流都不满足承诺，不切换。普通上游即使发送同名头，只要没有配置可信合同也不能触发切换。此机制是受约束的应用层协议，不是 HTTP 通用代理自动重试任意 POST 的许可。

没有向客户端输出时，可用 JSON HTTP 错误报告失败；已经开始 SSE 后，只能尽力发送 `error` 事件并结束流，不能改写已经发出的 200 状态。`unknown` 不等于模型没有运行。

## 取消、慢消费者与边界

`DELETE /v1/runs/<request_id>` 要求同一租户 Bearer 凭据，活动任务返回 202；其他租户或不存在的任务返回 404，已完成任务返回 409。202 仅表示网关已收到取消，不承诺供应商已停止计费。网关设置取消事件并关闭上游 socket；本地替身下一次写入时可观察到断开。客户端直接关闭下游 socket，也会在网关下一次写入失败时触发上游关闭。

网关用单独生产线程解析上游帧，默认 `queue.Queue(maxsize=2)`；消费者写下游，队列满时生产线程等待而不继续读取。写超时默认 0.3 秒，演示慢消费者时为 0.5 秒。单次上游 socket 超时默认 1 秒。**队列容量、单帧大小、socket 超时是三项不同限制**；没有实现整个请求的绝对截止时间。

`run_stats(tenant, request_id)` 提供 `queue_peak/backpressure_waits/producer_stopped`。慢消费者实验使用真实 TCP 流和小接收缓冲，不消费响应体；观察队列到顶并出现等待，然后取消。队列有界不等于进程总内存严格等于两个帧：解析器持有当前帧，HTTP 库和内核还有缓冲，也不保证真实服务商停止生成或停止计费。

## 审计与未实现项

`Gateway.audit.jsonl()` 输出内存审计记录，字段白名单为 `time/event/request_id/tenant/route/attempt/reason/outcome/usage_state/reservation_units/actual_units`。不记录 prompt、生成文本、Authorization、上游凭据、原始异常字符串；HTTP server 默认访问日志关闭。测试会检查敏感标记没有进入日志。

没有 `/metrics`、`/admin` 或 `/health`。没有 TLS、公开网络身份系统、持久账本、重启恢复、跨实例去重、供应商账单接入、真实模型调用、整体 deadline、生产限流或独立安全审计。所有监听和上游限定环回地址；内存账本与审计随进程退出丢失。实验通过只能证明列明的本地协议行为，不能外推真实模型质量和云端账单保证。
