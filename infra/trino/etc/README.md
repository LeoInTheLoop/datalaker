# Trino 平台安全基线（readme R3）

JSON 不支持注释，规则说明放这里。

## 依赖链：TLS → 认证 → 授权

`password authenticator` **强制要求 HTTPS**，三者必须按顺序配，
不能跳过 TLS 直接做认证。启用认证后还必须配
`internal-communication.shared-secret`——单机也不例外。

## rules.json 的意图（对应 readme 11.4）

| 用户 | 权限 | 依据 |
|---|---|---|
| `admin` | 全部 | 平台管理员，仅用于运维 |
| `claw` | iceberg 全部 / postgres 只读 | **写权限极窄**：只能写 lake，源系统只读（第 8 节） |
| `analyst` | 仅 iceberg.gold 只读，phone/email 遮蔽 | **最小权限 + PII 默认遮蔽**（11.4） |
| 其他 | 仅 system 只读 | 默认无权限 |

`analyst` 看不到 bronze/silver —— 只有发布到 gold 的数据才对外可见（第 6 节）。

## 已知限制

- `_comment` 等未知字段会导致 Trino 启动失败（JSON 严格校验）
- 密码明文在 `password.db` 之外的地方不出现；生产须改初始密码（11.6）
- 这是 file-based 起步版；**R4 的 Policy Sync 会换成 OPA**，
  届时策略由 catalog 的 owner/tag 生成，不再手工维护此文件
