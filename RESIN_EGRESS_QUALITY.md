# Resin 出口接入与 Grok Build 降智规避

本文说明本分支如何把 Resin 粘性代理池接入 Grok2API，并利用真实 Grok 请求的生成阶段 token 速度和响应头等待时间自动轮换出口 IP；首 token 之前的 SSE 静默只记录失败，不触发轮换。

这套机制不运行独立探测器，不主动生成测试请求，也不调用 Resin 的租约删除接口。所有判断都附着在客户端原本发起的真实请求上；触发轮换时，只更新下一个请求使用的 Resin 账号身份，不重放当前请求，因此不会因为换 IP 造成重复生成或重复计费。

## 目标

部署中观察到一种可操作的质量信号：部分 Grok Build 出口返回 token 的速度异常高，同时生成质量明显下降。本分支将它作为“降智出口”的判定信号：

- 输出速度大于 `200 tokens/s`；
- 请求本身已经成功完成。

满足以上条件时，当前结果仍然原样返回客户端，但该账号下一次请求会使用新的 Resin 粘性身份，从而获得新的出口 IP。之后的真实请求继续监测；如果新出口仍超过阈值，会继续轮换。

## 整体链路

```mermaid
flowchart LR
    Client[客户端请求] --> Selector[Build 最近使用优先调度]
    Selector --> Account[选择 Grok 账号]
    Account --> Identity[稳定 EgressIdentity + ResinAccountSuffix]
    Identity --> Resin[Resin 粘性代理]
    Resin --> Grok[Grok Build / Web / Console]
    Grok --> Stream[实时 SSE 转发]
    Stream --> Observe[附着式质量观测]
    Observe -->|正常| Keep[保留当前后缀]
    Observe -->|高 token 速度 / 响应头超时 / 流静默| Rotate[事务更新 Resin 后缀]
    Rotate --> Next[下一次请求使用新 IP]
```

## Resin 代理模板

出口节点使用带 `{account}` 占位符的认证用户名，例如：

```text
socks5h://Default.{account}:PASSWORD@resin:2260
```

`{account}` 只能出现在代理认证用户名中。请求发出前，出口管理器会把它替换为非敏感、稳定的账号身份：

```text
{EgressIdentity}_{ResinAccountSuffix}
```

其中：

- `EgressIdentity` 是稳定、不可逆且不包含 OAuth/SSO 凭据的账号身份；
- `ResinAccountSuffix` 是数据库中保存的 16 位随机十六进制后缀；
- 后缀为空时使用原始稳定身份；
- 后缀改变后，Resin 会把它视为新的粘性账号，从而分配新的出口；
- 不需要查询、删除或继承 Resin 租约。

## Build、Web、Console 共享出口

同一上游登录可能在数据库中表现为三个 Provider 账号：

- `grok_build`
- `grok_web`
- `grok_console`

通过 `account_provider_links` 和 `web_console_account_links` 正式关联的账号共享同一个 `EgressIdentity`。轮换时，关系表确定的整个账号族会在同一个数据库事务中更新为相同的 `ResinAccountSuffix`。

这样可以避免以下错误状态：

```text
Build 已切换新 IP
Web / Console 仍使用旧 IP
Cloudflare clearance 与实际出口不一致
```

轮换只跟随数据库中的正式关系，不根据邮箱、名称或 User ID 猜测关联关系。

## 降智信号：流式 token 速度

对成功的 Grok Build 流式响应，网关记录：

- `first_token_ms`：首个实际生成 token 已写入并 Flush 给客户端的时间；
- `duration_ms`：请求总耗时；
- `output_tokens`：上游报告的输出 token 数。

生成阶段耗时为：

```text
measured_ms = duration_ms - first_token_ms
```

token 速度为：

```text
tokens_per_second = output_tokens × 1000 / measured_ms
```

轮换条件：

```text
measured_ms >= 100
tokens_per_second > 200

## 管理员强制质量实验接口

管理员认证后可调用 `POST /api/admin/v1/quality-tests/requests`。接口复用普通
Gateway 的响应、SSE、首 token、输出 token 和审计 finalize 链路；只跳过客户端
计费预扣，不会在接口层重新计算 token。

请求示例：

```json
{
  "provider": "grok_build",
  "account_id": 100956,
  "egress_node_id": 12,
  "proxy_username": "Default.100956.switch-a",
  "request": {
    "model": "grok-4.5",
    "stream": true,
    "input": [{"role": "user", "content": "画一个鹈鹕骑自行车的svg"}]
  }
}
```

`proxy_username` 是可选的，但在实验时建议显式填写；它直接覆盖 Resin
`{account}` 占位符，因此可以在同一个账号/同一个节点上手工切换不同出口身份。
`egress_node_id` 也是可选的，仅用于强制指定 Resin 节点。响应体（包括流式 SSE）
原样按普通推理接口转发，审计中仍记录实际账号、节点、首 token 和输出 token。
请求成功完成
```

不再设置 1 秒最短生成时长，但保留 `100 ms` 的最小有效测量窗口。`measured_ms < 100` 的样本直接忽略；`measured_ms >= 100` 且生成速度严格大于 200 tokens/s 时继续轮换。首 token 之前的 SSE 静默只记录 `upstream_stream_silent`，不触发 Resin 轮换。

失败流、客户端取消、无法解析的 token 数和不完整响应不会被当作“高速降智”样本。

## 响应头超时

### Grok Build

Build 的 Go HTTP Transport 使用：

```text
ResponseHeaderTimeout = 10s
```

它只限制等待上游 HTTP 响应头的时间。收到响应头后，响应正文和 SSE 流不会被这个 10 秒值中断。

### Grok Web 与 Console

Web 和 Console 使用浏览器 TLS 指纹客户端。该客户端保留原有的长请求能力，但额外在“获取响应头”阶段设置 10 秒计时器：

```text
browserResponseHeaderTimeout = 10s
```

响应头到达后计时结束，后续长流继续传输。如果 10 秒内没有响应头，错误会被统一分类为：

```text
response_header_timeout
```

Build、Web、Console 的该错误都会立即触发账号后缀轮换。

注意：10 秒是单次物理上游尝试的响应头上限。一次客户端请求如果启用了账号切换或代理池安全重试，审计中的总耗时可能超过 10 秒。

## 流式静默监测

Grok Build 收到 HTTP 200 响应头后，如果连续 60 秒没有收到任何完整的上游 SSE 事件，网关会：

1. 关闭当前上游响应体以解除阻塞；
2. 将错误记录为 `upstream_stream_silent`；
3. 如果下游尚未收到任何字节，返回 HTTP 504，而不是空的 HTTP 200；
4. 使用 `silent_stream` 信号轮换 Resin 后缀。

监测的是完整 SSE 事件，不是任意 TCP 分片。代理即使持续发送无意义的半包，也不能无限延长静默期限。

## 非流式慢响应头

成功的 Build 非流式请求如果等待响应头达到 60 秒，即使最终成功，当前结果仍会返回客户端，但随后使用 `slow_response_headers` 信号轮换后缀。

这个逻辑不重试当前请求，避免产生两次模型调用。

## 并发与迟到请求保护

请求发出时会携带当时读取到的 `ResinAccountSuffix`。轮换使用 compare-and-swap 语义：

```text
UPDATE ...
WHERE resin_account_suffix = expected_suffix
```

如果一个旧请求较晚结束，而数据库中的后缀已经被更新，它的轮换会得到冲突并被跳过。旧请求不能覆盖新请求已经选择的出口。

对于关联账号族，轮换事务还会：

- 获取账号关系变更锁；
- 锁定整个账号族；
- 一次更新 Build、Web、Console 后缀；
- 向每个 Provider 发布账号状态失效通知；
- 清除本机路由候选缓存，使下一次请求读取新后缀。

## 流式响应不会被累计到最后

质量观测不会改变 SSE 转发方式。网关仍然执行：

```text
上游 Read(chunk)
→ 下游 Write(chunk)
→ Flush()
→ 继续读取
```

首 token 时间只会在包含生成增量的数据已经写入并 Flush 后记录。审计和速度计算不会等待完整响应后再向客户端发送。

协议转换层最多缓存一个完整 SSE 事件，或为 stop sequence、工具参数兼容和 Anthropic Web Search 顺序保存有界数据；普通生成文本不会整流缓存。

## 调度策略

Grok Build 使用“最近使用优先”的账号选择策略：

1. 排除已经达到并发上限的账号；
2. 按 `lastSelectedAt` 降序；
3. 选择最近使用且当前未满载的账号；
4. 有并发时自然选择排序中的下一个空闲账号。

这样能让活跃账号持续产生真实质量样本，避免账号池很大时大量账号长期处于“从未测量”的状态。

## 典型日志

轮换成功：

```json
{
  "msg": "resin_account_suffix_rotated",
  "provider": "grok_web",
  "signal": "response_header_timeout"
}
```

迟到请求被跳过：

```json
{
  "msg": "resin_account_suffix_rotation_skipped_stale_request",
  "signal": "fast_stream"
}
```

支持的轮换信号包括：

| 信号 | 触发条件 |
|---|---|
| `fast_stream` | 成功 Build 流的生成阶段至少 100 ms，且速度大于 200 tokens/s |
| `response_header_timeout` | Build、Web 或 Console 单次等待响应头超过 10 秒 |
| `silent_stream` | Build 收到响应头后 60 秒没有完整 SSE 事件 |
| `slow_response_headers` | 成功的 Build 非流式响应头等待至少 60 秒 |

## 安全边界

- Resin 密码只保存在加密的出口节点配置中；
- Git 仓库不应提交生产 `config.yaml`、`.env`、数据库目录或代理凭据；
- `EgressIdentity` 和随机后缀不包含 OAuth token、SSO token、邮箱或密码；
- 日志只记录账号数据库 ID、Provider 和信号，不记录完整代理 URL；
- 当前请求不会因质量判断被自动重放；
- 轮换影响后续请求，不改变已经开始传输的响应。

## 关键代码位置

- Build token 速度观测与轮换信号：`backend/internal/application/gateway/service.go`
- 90 秒 SSE 静默监测：`backend/internal/application/gateway/stream_silence.go`
- Build 代理身份拼接：`backend/internal/infra/egress/trace.go`
- Web/Console 代理身份拼接：`backend/internal/infra/egress/manager.go`
- Web/Console 10 秒响应头计时：`backend/internal/infra/egress/tlsclient.go`
- 账号族事务轮换：`backend/internal/infra/persistence/relational/account_repository.go`
- 流式实时写入与 Flush：`backend/internal/transport/http/inference/handler.go`
