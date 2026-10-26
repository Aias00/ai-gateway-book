# 附录 B：来源、版本与复核方法

S01—S10 第一方资料于 2026-10-21 读取；S11 源码于 2026-10-25 在本地固定提交核对。标准按节号定位；持续更新的产品文档保留读取日期。更详细的结论边界保存在仓库 `research/sources.md`。实验采用自己的协议，不表示符合所有真实供应商接口，也不提供价格或可用性承诺。

## HTTP 与事件流

**S01，RFC 9110，HTTP Semantics，第 9.2.2 节。** <https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2>。用于核对幂等与自动重试语义。请求没有返回响应，不能证明服务端没有应用它；代理不得自动重试非幂等请求。实验中的受控应用编排不能泛化为透明代理重放。

**S02，RFC 9112，HTTP/1.1，第 7.1、8、9.2 节。** <https://www.rfc-editor.org/rfc/rfc9112.html>。用于区分传输完整与应用完成，以及已经开始的最终响应所能表达的错误。HTTP/2 和 HTTP/3 的帧与流规则需要另行核对。

**S03，WHATWG HTML，Server-sent events。** <https://html.spec.whatwg.org/multipage/server-sent-events.html>。用于核对 UTF-8、行、空行、多行 data、事件标识和 EventSource 的处理模型。POST 自行读取流的客户端不会仅凭 SSE 编码继承 EventSource 自动重连行为，SSE 也不提供模型续写或业务撤销保证。

## 网关与观测

**S04，Envoy，Timeouts。** <https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/timeouts.html>。用于区分连接、空闲、路由与尝试期限。latest 文档可能对应开发版本，实际部署要重新核对所用版本。

**S05，Envoy，Router filter。** <https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/router_filter.html>。用于核对尝试预算、退避与错误类别的不同处理。产品提供重试配置，不等于业务已经具有幂等性。

**S06，OpenTelemetry GenAI semantic conventions。** <https://github.com/open-telemetry/semantic-conventions-genai/tree/8538971bd11dba723fdd33c804b5a32121827ff7>。本书核对的是此固定提交中的 client-inference 与内容捕获说明。相关语义在读取时标为 Development；可以参照设计请求模型、实际响应模型与用量字段，但不应声称这些字段永久稳定。提示词与输出内容具有敏感性，内容捕获需要明确选择和治理。

## 供应商协议差异

**S07，Anthropic，Streaming Messages。** <https://platform.claude.com/docs/en/build-with-claude/streaming>。流有消息、内容块、增量和终止等层次；部分 usage 为累计值，工具参数增量可能不是完整 JSON。该结论不能直接套到其他协议。

**S08，Anthropic，Rate limits。** <https://platform.claude.com/docs/en/api/rate-limits>。用于理解请求速率、输入和输出 token 限制的不同维度。限额不是保证获得的吞吐，限流口径也不自动等于费用口径。

**S09，Gemini，Models API。** <https://ai.google.dev/api/models>。模型元数据提供名称以外的信息，例如输入、输出限制与生成方法。一个生成方法字段不足以证明工具、结构化输出和所有其他能力兼容。

**S10，Amazon Bedrock，ConverseStream。** <https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseStream.html>。统一调用接口仍有模型能力差异；消息停止、metadata 与 usage 应按实际事件协议解释。该服务的事件流不能直接当作 SSE 使用。

## 开源实现案例

**S11，Agentgateway 固定源码。** 提交 `1785d189bbbd29bc87630030c3c3e1c91fda694e`，本地读取日期 2026-10-25。架构依据：<https://github.com/agentgateway/agentgateway/blob/1785d189bbbd29bc87630030c3c3e1c91fda694e/architecture/configuration.md>、<https://github.com/agentgateway/agentgateway/blob/1785d189bbbd29bc87630030c3c3e1c91fda694e/architecture/cel.md>。实现入口：<https://github.com/agentgateway/agentgateway/blob/1785d189bbbd29bc87630030c3c3e1c91fda694e/crates/agentgateway/src/app.rs>、<https://github.com/agentgateway/agentgateway/blob/1785d189bbbd29bc87630030c3c3e1c91fda694e/crates/agentgateway/src/proxy/httpproxy.rs>、<https://github.com/agentgateway/agentgateway/blob/1785d189bbbd29bc87630030c3c3e1c91fda694e/crates/agentgateway/src/mcp/README.md>、<https://github.com/agentgateway/agentgateway/blob/1785d189bbbd29bc87630030c3c3e1c91fda694e/crates/agentgateway/src/llm/README.md>。用于第 18 章的模块分工、配置内部表示、CEL 上下文和协议适配讨论。源码观察不代表本书已运行该工程，也不能证明真实上游兼容性、性能、多副本预算一致性或远端取消。详细定位见仓库研究记录。

## 使用这些资料的方法

接入真实服务时，先固定模型、协议与 SDK 版本，再建立最小契约样例。记录哪些能力实际跑过，哪些仅从文档获知，哪些仍未验证。对费率、账户限额和模型别名等可变化内容，不把某次读取值写成长期常量。

HTTP 标准解释传输与方法语义，供应商合同解释接受、去重与用量，业务系统解释任务是否完成。本书的测试只证明本地实验覆盖到的行为。把这些依据分别保存，才能在接口升级后知道哪些结论需要重新验证。
