# 第 15 章：并发结束了，预算为什么还被占用

完成、取消和结算是不同维度。一个运行已经没有本地工作线程，仍可能欠缺上游最终用量；一个取消请求得到 202，也不意味着远端费用已经停止。本章把这些差异变成可以读取的账本记录，而不是藏在一行“请求失败”里。

## 15.1 一次预留，两个限制

测试 `test_atomic_tenant_concurrency_admission` 将租户并发上限设为 1，先从真实客户端读到首个 token，再发第二个请求。后一个请求返回 429，替身只接到第一个请求。等待“首个事件已被读取”，比凭固定 sleep 猜测服务已经开始更可靠。

预算限制与并发限制分别判断，却必须在同一次原子接纳中检查。`test_parallel_reservation_cannot_overspend` 同时启动六个客户端，验证余额不会变成负数；最终允许多少个成功请求可能受结算时机影响，测试不会把某一个线程调度顺序写成合同。

```sh
python3 -m unittest discover -s tests -v -k atomic_tenant
python3 -m unittest discover -s tests -v -k parallel_reservation
```

两项实际运行均为 `ok`。改参数时，可把 `TenantPolicy(max_concurrent=1)` 改成 2，再观察原先的第二个请求是否能够进入。预算仍需足够，不能把 402 的额度拒绝误判为并发控制没有生效。

## 15.2 先核算字节，再检查最终证据

在 `utf8-bytes-v1` 教学合同中，`hi` 占 2 credits，输出上限 100 时预留 102。预留尚未解除时，这 102 都不能用于承诺别的新请求；正常收到完整 usage 与 done 后，才按实际输入和输出计数结算并释放差额。

不要用 Python 的 `len("你好")` 当作 UTF-8 字节数。运行 `test_real_http_stream_and_trusted_byte_usage` 时，输入“问题”占 6 字节，输出“你好 AI”占 9 字节，实际结算为 15。真实 tokenizer 与收费规则并不在这个实验里，字节合同只是为了让每个数字都能独立复核。

`test_missing_or_false_usage_cannot_settle` 分别制造缺失与矛盾的用量；`test_output_text_is_data_and_never_accounting` 则让模型正文看起来像一段 usage JSON。前两者保留未知状态，后者仍按正文真实字节核算。请求本身添加 `usage` 或 `tenant` 字段会被拒绝，客户端不能给自己指定身份或免单。

## 15.3 看见预留被保留下来

完整 demo 中断流任务的输出上限是 20，因此预留为 22。上游发送 `part` 后断开，没有最终 usage 和 done。实跑记录中的余额如下：

```json
{
  "balance_held": {
    "budget_units": 110,
    "spent_units": 0,
    "held_units": 22,
    "available_units": 88,
    "active": 0
  },
  "balance_after_reconciliation": {
    "budget_units": 110,
    "spent_units": 6,
    "held_units": 0,
    "available_units": 104,
    "active": 0
  }
}
```

前半部分表示本地并发名额已经释放，但 22 credits 仍被冻结。后半部分只有在演示程序利用受控替身的执行计划取得可信的 6 credits 证据后才出现：输入 `hi` 两字节，加实际执行输出 `part` 四字节。

演示调用的是进程内 `Ledger.reconcile("tenant-a", "interrupted", 6)`，不是让客户端填一个数字便自动退款。真实部署需要从供应商账单、运行查询或授权人工核对取得证据。这个方法没有暴露为 HTTP 接口，也没有实现账单同步。

## 15.4 取消传播要观察两端

完整 demo 的 `cancel_and_concurrency` 记录中，第二个并发请求得到 429，取消请求得到 202，上游替身观察到连接断开，而账本最终为 `cancelled/unknown`：

```text
concurrent_http_status = 429
cancel_http_status = 202
upstream_observed_disconnect = true
reservation_units = 1002
usage_state = unknown
active = false
```

请求路径是 `DELETE /v1/runs/<request_id>`，凭据必须属于同一租户。`test_cancellation_is_tenant_scoped_and_propagates_upstream` 先用另一个租户取消，验证返回 404 且原任务仍活动，再用所有者取消。202 只确认请求已接纳；最终状态仍应从运行结果和账本确认。

直接关闭客户端 TCP 也有对应测试。网关在下一次写入失败后关闭上游 socket；如果客户端只是停止读取而不关闭，仍可能先填满各层缓冲，再触发等待或写超时。这与供应商明确确认“停止执行且不再计费”不是同一件事。

## 15.5 用慢客户端把缓冲限制显露出来

```sh
python3 -m unittest discover -s tests -v -k slow_consumer
```

本版实际通过 `test_slow_consumer_has_bounded_event_queue_and_backpressure`。demo 记录的 `queue_peak` 为 2，`backpressure_observed` 为 true。测试缩小客户端接收缓冲，暂停读取大量输出，等待应用队列发生阻塞，再取消并确认上游看到断开。

可以把 `Settings(queue_events=2)` 改为 1，预期队列峰值不超过 1，仍能观察到等待。不要据此推算进程只会占用一个事件的内存：解析器、HTTP 库与内核都有额外缓冲。也不要把这里的等待时长写成生产吞吐或费用收益。

所有预算和审计都保存在单进程内存中，退出即丢失。完成这些实验以后，能够清楚说出“哪个限制生效、哪条证据还缺失”，才有依据继续增加持久账本、独立对账和多实例接纳；当前程序并没有提供这些生产保证。
