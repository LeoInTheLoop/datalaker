"""Production monitor retry cadence must remain unchanged in demo."""
import pathlib
import sys
import unittest
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

class MonitorWakeSuppression(unittest.TestCase):
    """被 WIP 挡回的线不该每分钟把模型叫醒一次。

    monitor 靠输出哈希抑制重复唤醒，而 `waited` 是其中唯一会变的量 ——
    它的格子必须够粗，否则哈希每次都不同，抑制永远失效。
    实测 2026-09-13：demo 环境把格子设成 60（调试值），被 WIP 挡回的
    ingest_table 每 2 分钟被唤醒重试，29 个 turn 里 16 个白烧在同一堵墙上。
    """
    def test_the_retry_grain_defaults_to_an_hour(self):
        import importlib, os
        previous = os.environ.pop("CLAW_RESUME_UNIT_SECONDS", None)
        try:
            import ops.resumable as resumable
            importlib.reload(resumable)
            self.assertEqual(resumable.RETRY_UNIT_S, 3600)
            row = {"updated_at": __import__("time").time() - 120}
            # 两分钟前更新过：同一格里必须逐字节相同，否则 monitor 会再叫一次。
            self.assertEqual(resumable._waited(row), resumable._waited(row))
            self.assertTrue(resumable._waited(row).endswith("h"))
        finally:
            if previous is not None:
                os.environ["CLAW_RESUME_UNIT_SECONDS"] = previous

    def test_the_demo_environment_does_not_ship_the_debug_grain(self):
        compose = (ROOT / "infra/docker-compose.demo.yml").read_text(encoding="utf-8")
        for line in compose.splitlines():
            stripped = line.strip()
            if stripped.startswith("CLAW_RESUME_UNIT_SECONDS"):
                self.fail(f"演示环境不该覆盖重试格子：{stripped}")


if __name__ == "__main__":
    unittest.main()
