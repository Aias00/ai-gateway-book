# 第 13 章：先运行一条完整的 HTTP 链路

这一章先不连接真实模型。我们需要看清三个独立角色：客户端发送请求并解析 SSE，网关决定接纳、路由和记账，上游替身通过真实 HTTP 返回内容与故障。替身提供确定的行为，连接、分块、断开和并发仍由真实网络栈处理。因此，这不是把路由函数调用一次便宣称集成完成。

## 13.1 从仓库根目录开始

实验只使用 Python 标准库，代码要求 Python 3.10 或更新版本，不需要模型密钥、SDK 或外网。先确认当前目录同时包含 `lab/` 和 `tests/`。本版记录的开发机运行环境是 Python 3.14；支持范围还应以你实际运行的版本和 CI 记录为准。

```sh
python3 --version
python3 -m unittest discover -s tests -v
python3 -m lab.demo --evidence lab/evidence/demo.json
```

测试会自己选择空闲的环回端口，并在结束时关闭服务。不要把文件直接当作脚本执行，例如 `python3 lab/demo.py`；它使用包内相对导入，应从仓库根目录用 `-m lab.demo` 运行。

所有监听都限制在 `127.0.0.1`。这个选择让实验服务留在本机，并不为同机其他用户建立生产级信任边界；请保持公开测试凭据与真实业务凭据分开。

完整 demo 将五组实验串起来：可信且获授权的切换、输出后断流、取消和并发、慢消费者、审计字段。`lab/evidence/demo.json` 是这一次运行保存的结构化记录。它不是从书里复制出来的预期答案；每组都有断言，未满足观察条件时命令会以错误结束。

下面节选自本版实际运行结果，省略了时间、余额与其他字段：

```json
{
  "name": "authorized_safe_failover",
  "http_status": 200,
  "events": ["token", "token", "usage", "done"],
  "upstream_operations": [0, 1],
  "accounting": {
    "reservation_units": 102,
    "actual_units": 13,
    "usage_state": "settled",
    "outcome": "completed",
    "active": false
  }
}
```

这里的 `[0, 1]` 由两个上游服务自己的执行计数产生。它说明第一个服务没有开始，第二个服务执行了一次，不能解释成两个真实模型的质量或性能比较。

## 13.2 用 curl 观察事件边界

要逐行观察流，先在一个终端保留服务：

```sh
python3 -m lab.demo --serve --port 8088
```

另开终端发送请求。`tenant-a-secret` 是仓库中公开的实验凭据，不应替换成真实供应商密钥：

```sh
curl -N http://127.0.0.1:8088/v1/generate \
  -H 'Authorization: Bearer tenant-a-secret' \
  -H 'Content-Type: application/json' \
  -d '{"request_id":"first","prompt":"hi","capabilities":["text"],"max_output_units":100}'
```

实际响应如下。事件之间的空行参与协议解析，不只是排版：

```text
event: token
data: {"text":"hello"}

event: token
data: {"text":" gateway"}

event: usage
data: {"contract":"utf8-bytes-v1","input_units":2,"output_units":13}

event: done
data: {}
```

这个交互模式使用正常文本替身；它与完整 demo 的故障计划不同，所以两处实际输出量并不相同。`hi` 占 2 个 UTF-8 字节，`hello gateway` 占 13 个，完整使用量为 15 credits。100 是允许的输出上限，而不是已经发生的消耗。

按 Ctrl-C 退出服务时，终端会显示审计 JSONL。可以看到 `admitted`、`route_attempt`、`finished` 和最终结算，却看不到输入正文、生成正文或 Authorization。审计不是聊天记录的另一个副本。

## 13.3 改一个参数，再解释变化

把 `request_id` 改为 `short`，将 `max_output_units` 从 100 改为 3，再发一次请求。正常替身的第一个文本块被限制为 `hel`，随后给出输出用量 3 和 `done`；输入仍然为 2。这一实验检验输出边界与计量是否一致，不是要求真实模型按字符数控制生成。

接着保持同一进程，重复发送第一次的 `request_id=first`。实际返回为 HTTP 409：

```json
{"error": "duplicate_request_id"}
```

网关拒绝重复启动上游，不会把之前的正文重新播放给你。换用新 ID 可以表达新任务，但不能用它掩盖对同一业务动作的重复执行；本实验没有持久幂等存储，进程重启后这些 ID 会丢失。

## 13.4 成功路径之外还要看什么

测试输出里的 `test_real_http_stream_and_trusted_byte_usage` 核对中文 UTF-8 与完整结算；`test_duplicate_id_does_not_duplicate_upstream_side_effect` 核对上游执行次数；`test_audit_is_useful_without_prompt_tokens_or_keys` 检查敏感标记没有进入日志。只看终端出现 `hello`，不能代替这三类证据。

如果提示地址被占用，可换一个端口并同步修改 curl 地址。如果返回 401，先检查实验 Bearer 字符串；如果返回 400，检查字段名、类型和请求大小。如果测试报超时，应保留完整失败输出，确认是否被本机网络策略阻止环回连接，而不是先把断言删除。

这一条链路建立后，下一步才是有控制地制造失败，观察网关在没有足够证据时会如何停止。
