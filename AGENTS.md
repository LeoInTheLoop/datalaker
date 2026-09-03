# AGENTS.md

本项目的 agent 约定与开发规范集中在 **[CLAUDE.md](CLAUDE.md)**，请先读它。

## 这是什么

**Data Steward Claw** —— 一个以数字员工形态落地的数据管家：
连接散落的源系统，通过邮件与人确认口径和审批，
在独立 lakehouse 中整理出 one source of truth，并同步理清权限。

完整设计见 [readme.md](readme.md)（20 节），
行业数据支撑见 [docs/industry-context.md](docs/industry-context.md)。

## 开工前必读

| 文件 | 内容 |
|---|---|
| [CLAUDE.md](CLAUDE.md) | **五条铁律**、目录结构、环境约束、测试命令 |
| [docs/handoff/](docs/handoff/) | 每阶段交接记录，**含「不要做的事」清单** |
| readme 第 19 节 | 实现状态表——哪些跑通了、哪些还是纸上的 |

## 五条铁律（详见 CLAUDE.md）

1. 限制一律走 hook，不写进 prompt
2. Agent 对 `decisions` 表无写权限，审批 callback 必须独立进程
3. 源系统只读，且源库负载必须经 SQL gate 控制
4. 不修改源系统
5. 新工具必须在 `policy.py` 显式声明——**未声明的一律拒绝**

## 快速开始

```bash
cd infra && docker compose --env-file ../.env --profile core --profile agent up -d
HERMES=<hermes-agent 路径> ./tests/run_all.sh     # 538 条断言必须全绿
python3 ops/claw-status.py                        # 运维面板
```

> ⚠️ Docker 数据盘在外置硬盘上，Mac 睡眠后会掉线。
> `docker ps` 超时、daemon 连不上或报 I/O error 时**先查盘**：
> `diskutil list external physical` + `ls /Volumes`。
> 如果盘已挂载，优先试这个恢复顺序：
> `open -a Docker`，等 `docker ps` 通，再执行
> `cd infra && docker compose --env-file ../.env --profile core --profile agent up -d`。
> 这是待验证经验；下次不管用就删掉这条。
