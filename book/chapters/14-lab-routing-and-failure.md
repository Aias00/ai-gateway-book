# 第 14 章：让切换发生在有证据的边界

路由测试要回答两个不同的问题：哪个上游有资格执行，当前请求是否允许选择下一上游。能力标签解决第一个问题；授权与上游执行证据共同限制第二个问题。把它们混成“失败就找另一家”，会使一次调用悄悄变成重复执行。

## 14.1 先检查能力，再接纳请求

运行下面的单项集成测试：

```sh
python3 -m unittest discover -s tests -v -k capability_filter
```

本版实际结果为：

```text
test_capability_filter_precedes_admission ... ok
```

测试建立两个 HTTP 上游。第一个只声明 `text`，第二个声明 `text` 和 `image`。请求 `image` 时，第一个上游的接收次数为 0，第二个为 1；请求无人支持的 `audio` 时，网关返回 422，且没有建立预算预留。

这里的 `image` 是测试路由集合关系的标签；替身没有处理图片或完成视觉推理。实验验证的是资格判断发生在上游调用之前，不能据此宣传已经实现多模态模型服务。真实接入还需要核对实际输入结构、模型配置和供应商能力。

## 14.2 默认不切换，必须明确授权

下面的短程序创建两个独立服务。保持 `allow_failover=False` 运行一次，再改成 `True` 重新运行。每次脚本都会启动新的进程内账本与上游计划，不存在上一轮余额或故障计划污染：

```python
from lab.client import generate
from lab.gateway import Gateway, Route
from lab.upstream import ModelStub, Plan

allow_failover = False
with ModelStub([Plan("safe_503")]) as first, ModelStub() as backup:
    routes = [Route("first", first.url, trust_not_started=True),
              Route("backup", backup.url)]
    with Gateway(routes) as gateway:
        status, result = generate(gateway.url, {
            "request_id": "routing-case", "prompt": "hi",
            "capabilities": ["text"], "max_output_units": 100,
            "allow_failover": allow_failover,
        })
        print(status, first.requests, first.operations_started,
              backup.requests, backup.operations_started)
```

实际输出的两种结果分别是：

```text
# allow_failover=False
503 1 0 0 0
# allow_failover=True
200 1 0 1 1
```

第一个服务明确返回 HTTP 503 和 `X-Lab-Execution: not-started`。这个头只在当前替身协议里承诺“没有开始执行，也没有计费”，服务端又显式配置了信任。即便如此，缺少请求自身的切换授权，网关仍不会启动后备服务。

把 `trust_not_started` 改为 `False`，再将请求设为允许切换，结果仍为 503，后备次数仍为 0。一个来自未建立合同的上游的自称，不能成为新的执行权限。

## 14.3 普通错误与未知执行结果

将故障计划改成 `Plan("unknown_503")`。这个替身先增加执行计数，再返回普通 503；即使请求允许切换，后备服务也不会被调用。它模拟“响应失败但工作可能已经发生”的窗口。

另一个反例是 `Plan("pre_event_disconnect")`：上游已接受请求，却在发出任何完整 SSE 事件前断开。客户端看不到 token，不代表上游没有开始，也不能推出费用为零。当前实验甚至对连接建立失败也不自动选择下一上游，以保留唯一且容易审查的切换入口。

`Settings.max_attempts=2` 只是上限，不是必须用满的次数。三个候选服务中，前两个均给出受信任的未开始拒绝时，第三个仍不会启动。这个性质由 `test_failover_has_finite_attempt_bound` 核对，避免配置更多路由以后无意扩大尝试次数。

## 14.4 输出以后，不能拼接一段新的生成

运行断流测试：

```sh
python3 -m unittest discover -s tests -v -k midstream_disconnect
```

实际结果中，`test_midstream_disconnect_reports_error_without_replay` 通过。完整 demo 保存的对应记录节选为：

```json
{
  "name": "interrupted_not_replayed",
  "http_status": 200,
  "events": ["token", "error"],
  "backup_requests": 0,
  "accounting_before_reconciliation": {
    "reservation_units": 22,
    "actual_units": null,
    "usage_state": "unknown",
    "outcome": "failed",
    "active": false
  }
}
```

200 已经发给客户端，后续错误不能改成新的 HTTP 状态。网关尽力发送 `error` 事件，终止当前流，保存未知结算；它不会接着从第二个模型取得文字再拼在前半段后面。客户端需要同时检查流的终止事件，不能仅以最初的 200 判定成功。

## 14.5 网络分块不等于模型事件

`test_sse_line_endings_multidata_and_fragmented_utf8_over_http` 将中文字符拆在不同 HTTP 块，使用 LF、CRLF、CR 行结束，并把 JSON 拆成多条 `data` 行。三个变体都能还原一个完整事件。另一方面，无空行就到 EOF 的帧不会派发，超过 32 KiB 的帧会失败；相应测试名称包含 `eof_without_empty_sse_line` 与 `oversized_sse_frame`。

这套解析器要求命名的 event 与 JSON 对象，不实现 id、retry 或自动重连。练习时先改坏帧的结束空行，再恢复它；应当看到上游确实接收了请求，但网关没有把半条内容转发成有效 token，账本也不会伪造成功。

CR-only 的空行到达就应派发，不等待后续内容。专门的 `test_cr_only_complete_event_is_dispatched_before_any_later_bytes` 用同步事件扣住后续数据，客户端必须先读到首个 token 才放行。大小计量对 CR 预留可能跟随的 LF 字节，因此临界大帧可能被保守提前拒绝，不会漏算而越过上限。
