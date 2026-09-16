#!/usr/bin/env python3
"""把 `infra/claw.yaml` 的角色写进治理库。**装机时跑一次，换人时再跑。**

    CLAW_ADMIN_DSN=postgresql://postgres:...@127.0.0.1:5432/steward \
        python3 ops/claw-init.py            # 写入
    python3 ops/claw-init.py --check        # 只校验文件和库，不写

**为什么是单独一个脚本，而不是网关启动时顺手做掉：**
指派审批人是运维决定。Agent 容器只拿到 `agent_role`，网关启动时只做
`verify()`（只读）；角色表对不上就拒绝启动，而不是自己补一行。
让被约束的进程去 seed 约束自己的那张表，形状上就是「自己批准自己」的邻居
（执行边界 2）。

没有 DSN 时落到本地 SQLite，和 `open_store()` 的选择规则一致 ——
无 docker 的快测也能有个正确的起点。
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services"))
sys.path.insert(0, str(ROOT / "plugins"))

import claw_init                                             # noqa: E402
from datasteward_gate.approvals import open_store            # noqa: E402


def main() -> int:
    check_only = "--check" in sys.argv[1:]
    try:
        config = claw_init.load()
    except claw_init.ClawInitError as exc:
        print(f"初始化文件不可用：{exc}", file=sys.stderr)
        return 2
    print(f"读取 {config['path']}")
    print(f"  {config['agent']['name']} @ {config['organization']['name']}"
          f" · {config['email']['agent_email']} · 审批档位 {config['approval']['level']}")

    # `open_store()` 按 DATASTEWARD_DSN 选后端。这里显式换成 admin 连接：
    # 写 role_assignment 是运维动作，不该借 Agent 的账号完成。
    admin = os.environ.get("CLAW_ADMIN_DSN") or os.environ.get("DEMO_ADMIN_DSN")
    if admin:
        os.environ["DATASTEWARD_DSN"] = admin
    elif os.environ.get("DATASTEWARD_DSN"):
        print("拒绝用 DATASTEWARD_DSN 写角色表：那是 Agent 的账号。"
              "请设 CLAW_ADMIN_DSN。", file=sys.stderr)
        return 2

    store = open_store()
    try:
        if check_only:
            claw_init.verify(store, config)
            print("库里的角色与初始化文件一致")
            return 0
        changed = claw_init.bootstrap(store, config)
        # 写完立刻读回来比对。**不自验的写入等于没写** —— 权限不足时
        # 有的后端只是静默少影响一行，而「跑完没报错」看着就像成功了。
        applied = claw_init.verify(store, config)
    except claw_init.ClawInitError as exc:
        print(f"自验失败：{exc}", file=sys.stderr)
        return 1
    finally:
        store.close()

    print("已指派：" + ("、".join(changed) if changed else "无变化（已是声明的状态）"))
    print("当前生效：" + "、".join(applied))
    return 0


if __name__ == "__main__":
    sys.exit(main())
