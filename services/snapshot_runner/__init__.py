"""Snapshot Runner —— 把一次真实实测从起点跑到证据归档。

模块清单与用法见同目录 README.md。这里只做一件事：让
`services/` 与 `services/snapshot_runner/` 两种 import 写法都能用，
因为容器把 `services/` 直接挂在 `/app/services`，而 Hermes 插件是以
`services/` 为 sys.path 根加载 hook 的。
"""
