# 资料核对与引用边界

本书是一套教学用网关实验，不代表任何供应商的 SLA、价格或账单承诺。以下来源均已于 **2026-10-21** 实际读取；标准按节号定位，持续更新的文档按页面标题与小节定位。正文引用可使用 `[S01]` 至 `[S10]`，并链接到本页。

本页区分三件事：标准明确规定的行为、某一产品文档描述的行为，以及本书为了实验可复现而选择的策略。不能把第三种写成前两种，也不能把某一供应商的字段解释套用到所有兼容接口。

## S01 · RFC 9110：HTTP Semantics

- URL：<https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2>
- 实际读取的文本版：<https://www.rfc-editor.org/rfc/rfc9110.txt>
- 读取日期：2026-10-21。
- 定位：§9.2.2 “Idempotent Methods”。
- 可以支持的结论：幂等关注的是重复请求对服务器的**预期效果**，不要求返回内容相同，也不排除各次请求分别产生日志。客户端不应自动重试非幂等方法，除非有办法知道其实际语义幂等，或确认原请求从未被应用。该节另明确规定：代理不得自动重试非幂等请求。
- 在本书中的用法：重试决策先判断语义和请求阶段，再判断错误码。教学模型的纯文本生成、真实模型的按次计费、工具执行与业务写入必须分别讨论。网关生成一个 request ID 或接受一个 `Idempotency-Key`，不会自动赋予上游持久去重能力。
- 不能据此声称：HTTP `POST` 因为“只是生成文本”就天然允许代理透明重试；第一次没收到响应就等于上游没执行；`temperature=0` 保证重复请求只计费一次；超时能证明业务失败。

## S02 · RFC 9112：HTTP/1.1 消息完整性

- URL：<https://www.rfc-editor.org/rfc/rfc9112.html#section-8>
- 实际读取的文本版：<https://www.rfc-editor.org/rfc/rfc9112.txt>
- 读取日期：2026-10-21。
- 定位：§7.1 “Chunked Transfer Coding”、§8 “Handling Incomplete Messages”、§9.2 “Associating a Response to a Request”。
- 可以支持的结论：HTTP/1.1 分块编码的结束与应用事件的结束是两个层次。未收到结束分块，或接收字节少于有效 `Content-Length` 时，消息不完整。客户端收到提前断开的响应必须记录为不完整。HTTP/1.1 一个请求只有一个最终响应，最终响应之前可以有 1xx 信息响应。
- 在本书中的用法：已发给客户端的最终响应状态和正文不能收回；之后发生的流错误应使用事先约定的应用错误事件、明确终止，或连接错误表达。不能在已经发送的 `200` 后面再尝试发一个 `502`。实验必须同时检查传输是否结束、协议终止事件是否到达、usage 是否取得。
- 不能据此声称：这些 HTTP/1.1 分块规则原样适用于 HTTP/2、HTTP/3；连接正常关闭必然代表模型生成成功；客户端断开连接等于上游生成已停止、工具已撤销或费用已退回。RFC 的消息与连接规则没有提供业务回滚协议。

## S03 · WHATWG HTML：Server-sent events

- URL：<https://html.spec.whatwg.org/multipage/server-sent-events.html>
- 读取日期：2026-10-21。
- 定位：§9.2.2 “The EventSource interface”、§9.2.3 “Processing model”、§9.2.4 “The `Last-Event-ID` header”、§9.2.5–9.2.6 流格式及解析。
- 可以支持的结论：SSE 为 `text/event-stream`，以 UTF-8 编码；行结束可以是 CRLF、LF 或 CR，空行触发事件派发，多行 `data` 以换行拼接。流在事件末尾空行到来前结束，该不完整事件不应被派发。浏览器原生 `EventSource` 有重连模型，`id` 与 `Last-Event-ID` 用于携带最后事件标识；`close()` 终止该对象发起的 fetch 并将状态置为 CLOSED。
- 在本书中的用法：解析器按**字节流增量解码、行、事件**三个层次工作，不能把一次 TCP 读取或一次 HTTP chunk 当作一个 JSON 事件。终止标志如 `[DONE]`、`message_stop` 是上层协议约定，不是 SSE 通用规范。
- 不能据此声称：SSE 自动提供消息存储、去重、恰好一次投递或模型续写；接上 `Last-Event-ID` 就能恢复上一次生成；采用 `fetch()` 发送 POST 并自行读流的客户端继承了原生 `EventSource` 的自动重连行为；`EventSource.close()` 保证服务端业务回滚。

## S04 · Envoy：超时的不同作用范围

- URL：<https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/timeouts.html>
- 读取日期：2026-10-21。
- 定位：“Stream timeouts”“Route timeouts”。读取页面显示为 Envoy 开发版文档，示例部署应再核对所选正式版本。
- 可以支持的结论：连接空闲、流空闲、请求接收、完整响应期限、最大流生命周期、单次尝试超时分别约束不同阶段。路由 `timeout` 在整个下游请求接收完毕后开始，等待的是**完整上游响应**；流式端点需要根据实际最长响应时间调整。`per_try_timeout` 只适用于任何响应部分发到下游之前，通常该边界位于上游响应头转发时。
- 在本书中的用法：分别定义连接期限、首个有效响应等待期限、事件空闲期限、总 deadline 和慢客户端写入期限。只有心跳持续到来而业务文本不前进时，应明确心跳是否刷新“业务进展”计时器。关闭某个默认超时必须同时说明替代的资源边界。
- 不能据此声称：把路由超时设为 0 就适合所有流服务；持续收到字节就不会永远占用资源；本书 Python 实验计时器与 Envoy 计时器完全等价；响应已经部分到达客户端后还能借助 `per_try_timeout` 透明换模型。

## S05 · Envoy Router：重试预算与失败阶段

- URL：<https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_filters/router_filter.html>
- 读取日期：2026-10-21。
- 定位：“x-envoy-max-retries”“x-envoy-retry-on”“x-envoy-upstream-rq-per-try-timeout-ms”。
- 可以支持的结论：路由总超时包含各次尝试和退避，单次尝试不能重新获得完整总预算；Envoy 的重试策略可以区分连接失败、reset、未发出请求的 reset 及特定状态码；默认没有相应重试策略时，不会仅因请求失败就重试。文档还描述全抖动指数退避与基于响应头的退避配置。
- 在本书中的用法：计算 `remaining = deadline - now`，每次尝试、等待与退避都消耗同一个预算；限制尝试次数，并记录 attempt 序号、原因与已消耗预算。区分“本地 TCP 建连失败”与“服务端已受理后发生读超时”。
- 不能据此声称：配置 `5xx` 重试就满足业务幂等；所有 429 都有相同恢复方式；文档中的 Envoy 默认值就是本书教学网关默认值；同一请求 SDK、网关和服务端各重试一次不会放大请求量。Envoy 的可配置能力不能取代 S01 中的语义判断。

## S06 · OpenTelemetry：GenAI 观测语义

- 当前第一方仓库：<https://github.com/open-telemetry/semantic-conventions-genai>
- 固定读取版本：`8538971bd11dba723fdd33c804b5a32121827ff7`。
- 推理观测语义：<https://github.com/open-telemetry/semantic-conventions-genai/blob/8538971bd11dba723fdd33c804b5a32121827ff7/docs/gen-ai/client-inference.md>
- 内容捕获说明：<https://github.com/open-telemetry/semantic-conventions-genai/blob/8538971bd11dba723fdd33c804b5a32121827ff7/docs/gen-ai/gen-ai-spans.md#capturing-instructions-inputs-and-outputs>
- 读取日期：2026-10-21。
- 定位：`Span: gen_ai.client.inference`、`gen_ai.request.model`、`gen_ai.response.model`、`gen_ai.response.time_to_first_chunk`、`gen_ai.usage.*`，以及 “Capturing instructions, inputs, and outputs”。
- 可以支持的结论：请求模型与实际响应模型是不同观测字段；流式首块延迟需要明确从请求发起到哪个事件接收的时刻；usage 包括输入、输出和可能的缓存等细分。指令、输入与输出常有敏感信息并且体积大，内容捕获不应默认打开，应提供用户选择。
- 在本书中的用法：分开记录路由别名、实际选择的 provider/model、各 attempt、首个协议事件时间、首个正文时间与请求终态。实验中的 `first_text_ms` 不应改名为标准的 time-to-first-chunk 并宣称含义相同；默认结构化日志只记录元数据，不写提示词、正文和密钥。
- 不能据此声称：这些 GenAI 字段已经全部稳定；采集完整提示词是 OpenTelemetry 的强制要求；缺少某个 provider usage 字段可以填 0；遥测 token 数天然等于供应商账单。
- 版本提醒：原 <https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-spans/> 当前只保留迁移提示。读取的新仓库相关文档标记为 **Development**，正文应把字段写为“参照此版本设计”，不能声称未来版本不变。

## S07 · Anthropic：Messages 流事件与累计 usage

- URL：<https://platform.claude.com/docs/en/build-with-claude/streaming>
- 读取日期：2026-10-21。旧 `/docs/en/api/streaming` 地址重定向到上述地址。
- 定位：“Event types”“Error events”“Other events”“Input JSON delta”。
- 可以支持的结论：Messages 流有 `message_start`、content block 起止及 delta、`message_delta`、`message_stop` 等层次；`message_delta.usage` 中的 token 数为**累计值**。已建立的流可能通过 `event: error` 表达错误。工具参数 delta 可为不完整 JSON 片段，需要按内容块累计并在合适的结束点解析。文档提醒适配器能妥善处理新增未知事件类型。
- 在本书中的用法：累计 usage 不能逐事件求和；工具调用参数片段不能逐片段 `json.loads()`；HTTP 层 `200` 与模型完成是不同状态。把供应商事件适配为内部事件时，保留无法识别事件的诊断信息，避免误判为正文或终止。
- 不能据此声称：所有供应商的 usage 都是累计值；SSE 的一个 `data` 就是一段完整工具参数；普通代理能模拟供应商内部的续写或回退机制；断流时一定能取得最终 usage；没有 `message_stop` 就可把已收到文本作为完整成功返回。

## S08 · Anthropic：限流不是单一 RPM

- URL：<https://platform.claude.com/docs/en/api/rate-limits>
- 读取日期：2026-10-21。
- 定位：“About rate limits”“Rate limits”“Cache-aware ITPM”“Reaching your spend cap”。
- 可以支持的结论：该 API 区分请求速率、输入 token、输出 token 等限制，并可能按组织、工作空间和模型类别生效；短时突发也可能触发限流。文档描述 token bucket，并指出公布限额是最大允许使用量，不是保证可获得的最低吞吐。响应带 `retry-after` 时应按其等待；并非每个 429 都具有同样可重试语义。
- 在本书中的用法：本地租户配额和供应商账户配额是不同维度；状态码之外还要检查已记录、可识别的错误类型和响应头。教学价格、tokenizer 和预留算法要显式标注为本地策略；上游 token 用量、速率扣减口径和费用口径分别保存。
- 不能据此声称：固定“每分钟 N 次”就能完整保护上游；429 永远可短暂退避解决；本地 `max_output_tokens` 预留量就是供应商实际 OTPM 扣减量；缓存 token 的计费和限流规则对所有模型一致；文档读取日的等级、价格和限额永久有效。

## S09 · Gemini Models API：模型元数据与能力过滤

- URL：<https://ai.google.dev/api/models>
- 读取日期：2026-10-21。
- 定位：“Method: models.get”“Method: models.list”“Resource: Model” 的 `inputTokenLimit`、`outputTokenLimit`、`supportedGenerationMethods` 等字段。
- 可以支持的结论：模型列表与详情可提供模型名称之外的元数据，包括输入、输出 token 上限及支持的生成方法；路由配置应保留可验证的能力信息，而不是仅凭别名猜测兼容性。
- 在本书中的用法：先筛选协议、流式能力、工具调用、输入模态、上下文和输出格式，再在候选中按策略选择；元数据需要版本和更新时间。具体能力仍须结合对应模型/功能文档与契约测试确认。
- 不能据此声称：`supportedGenerationMethods` 一项足以证明全部工具与 JSON Schema 功能兼容；两个模型都支持 `generateContent` 就有相同输出、计费和故障语义；同名别名永远指向同一快照；本书 mock 的 capability 布尔值是供应商真实能力声明。

## S10 · Amazon Bedrock ConverseStream：统一接口仍有能力差异

- URL：<https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_ConverseStream.html>
- 读取日期：2026-10-21。
- 定位：页面概述、Request/Response Syntax、Response Elements 中的 `metadata`、`modelStreamErrorException`、`throttlingException`。
- 可以支持的结论：ConverseStream 提供统一消息流接口，但文档要求通过 `GetFoundationModel` 的 `responseStreamingSupported` 确认模型是否支持流；还允许模型特有推理参数。响应有内容块、消息和 metadata 等不同事件；metadata 的 usage 包含输入、输出、总 token 及缓存等细分；流中可能发生模型流错误或限流错误。
- 在本书中的用法：协议适配与能力匹配是两个步骤；完成正文、收到 `messageStop`、收到带 usage 的 metadata 应分开处理。中断后拿不到权威最终用量时，保留 `unknown/pending_reconciliation`，并根据供应商对账接口、账单或合同进行后续核对。
- 不能据此声称：Bedrock 流就是 SSE；消息停止与最终 metadata 必然在同一个网络读取块里；所有支持统一接口的模型都支持同一组功能；断流时 usage 必然会被客户端收到；`inputTokens + outputTokens` 足以复算所有费用；客户端取消就一定停止推理、停止计费或撤销已经发生的外部动作。

## 本书的四条边界

1. **响应提交边界**：发送给客户端的最终响应头/正文无法撤回。之后的失败不得伪装成一次新的、未中断的完整回答。若要支持恢复，必须另行定义持久事件日志、游标、去重与业务语义；SSE 自身不提供这些能力。依据：S02、S03、S04、S07。
2. **取消边界**：本地关闭上游连接是资源清理和尽力传播取消的动作。供应商执行状态、外部工具副作用和计费状态仍可能未知。除非另有已核验的业务取消接口及确认结果，不能记录成“上游已回滚”。依据：S01、S02、S03；这是这些规范保证范围之外的工程边界，并非标准给出的供应商行为保证。
3. **结算边界**：预留额度是本地准入策略，估算用量是估算，供应商最终 usage 是上游证据，账单是另一份需对账的证据。缺失 usage 必须有独立状态，不能用零值或可见字符数假装为真实账单。依据：S06、S07、S08、S10。
4. **兼容边界**：路径与 JSON 字段兼容只代表协议层的一部分。候选模型还需满足明确能力和当前租户权限，切换后质量也需评测。依据：S09、S10；质量通过与否由本书的回归集证明，不由接口文档证明。

## 复核办法

发布前或替换真实供应商适配器时，重新打开所引用的小节；对会变化的内容保留读取日期，不把具体模型、套餐限额或价格抄成长期常量。协议测试必须覆盖 UTF-8 跨读取边界、CRLF 跨读取边界、多行 data、不完整末事件、流内 error、累计 usage、终止前断流，以及未知事件。所有性能数字应附实验配置与测量方法；本页未提供任何真实供应商压测或可用性证据。
