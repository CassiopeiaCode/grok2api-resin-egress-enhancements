# Resin 健康出口池与 Grok2API Quality Guard 接入

本分支以 Grok2API v3.1.1 为主体，保留上游的 Web、Console、Image、Video、DPoP、
账号隔离连接池和 Egress Quality Guard，同时增加 Resin sticky username 粒度的健康池。

## 为什么仍需要健康池

Grok2API 只看到一个 Central Resin egress node，但不同 Resin 用户名对应不同粘性租约和
实际出口 IP。上游 Quality Guard 默认按 node 禁用；一个坏租约不应导致整个 Central Resin
节点以及其他健康租约一起下线。因此本分支把 Build 的生产调度细化到 Resin username。

## 判定方式

不再使用鹈鹕 SVG、图像特征或 KNN 分类器。主动探测采用上游 Quality Guard 的固定文本
Prompt、SSE 计时方式和审计 Token 口径：

```text
Write exactly 16 numbered lines about reliable distributed systems. Each line must be one complete English sentence, with no markdown heading. The final line must end with the exact marker QUALITY_OK.
```

候选租约必须满足：

- 响应包含 `QUALITY_OK`；
- 输出 Token 不少于 32；
- 能测得首 Token 和有效生成窗口；
- `outputTokens * 1000 / (durationMS - firstTokenMS) <= 200`；
- Cloudflare trace 能解析实际出口 IP。

超过 `200 Token/s` 的样本判坏。主动探测判坏时不入池；生产 Build 流同一 username
连续两次超过阈值时将其逐出租约池。

## 持久化

PostgreSQL 表：

- `pelican_egress_entries`：active good Resin username、exit IP、探测版本和时间；
- `pelican_bad_egresses`：按真实 exit IP 保存 24 小时临时黑名单。

名称继续保留 `pelican` 仅用于数据库/API 向后兼容，不再表示鹈鹕分类器。

重启后 good pool 和 bad IP 会从 PostgreSQL 恢复。连续高速次数、响应头超时次数属于短期
进程状态，重启后重新累计。

## 调度

池目标默认是 5。Build 账号通过稳定哈希选择 active good username，因此不同账号可稳定走
不同出口，同一账号在池成员不变时保持同一租约。OAuth refresh 也使用同一个健康池选择器，
避免模型请求走健康出口、刷新却走普通或损坏出口。

当池少于目标容量时，sidecar 会：

1. 生成新的随机 Resin username；
2. 通过 Cloudflare trace 获取真实出口 IP；
3. 跳过仍在 24 小时黑名单内的 IP；
4. 使用上游固定 Prompt 做真实 Grok Build 流式探测；
5. 只有满足 200 Token/s 阈值和完整性要求时才写入 good pool。

## 与上游 Quality Guard 的关系

上游功能完整保留，包括管理页面、主动/被动检测、系统探测身份和受限内部 API。对于普通
独立 egress node，可直接使用上游 node-level 隔离。Central Resin 的具体租约质量则由本分支
的 username-level pool 管理，避免禁用整个中央节点。

## 安全边界

- 不在日志中输出 Resin 密码、OAuth/SSO token 或 Client Key；
- username 只作为 Resin sticky identity，不发送给 Grok；
- bad IP 黑名单按 Cloudflare trace 的实际出口共享；
- 当前请求不会因为淘汰租约而重放，变更只影响后续请求；
- 管理员强制质量测试不会参与生产租约连续次数。
