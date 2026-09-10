#!/usr/bin/env python3
"""Deterministic model-expiry policy checks (no network or API key)."""
from __future__ import annotations

import importlib.util
import os
import pathlib
import unittest
from datetime import date


ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "agent_entrypoint", ROOT / "docker" / "agent-entrypoint.py"
)
agent = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(agent)


class ModelExpirationPolicy(unittest.TestCase):
    def setUp(self):
        self.old = {
            key: os.environ.get(key)
            for key in ("OPENAI_MODEL", "OPENAI_MODEL_FALLBACKS",
                        "DASHSCOPE_MODEL_EXPIRATIONS")
        }
        self.old_active = (agent.ACTIVE_MODEL, agent.ACTIVE_MODEL_EXPIRATION,
                           agent.MODEL_SELECTION_DETAIL)

    def tearDown(self):
        for key, value in self.old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        (agent.ACTIVE_MODEL, agent.ACTIVE_MODEL_EXPIRATION,
         agent.MODEL_SELECTION_DETAIL) = self.old_active

    def configure(self, primary, fallbacks, expirations):
        os.environ["OPENAI_MODEL"] = primary
        os.environ["OPENAI_MODEL_FALLBACKS"] = fallbacks
        os.environ["DASHSCOPE_MODEL_EXPIRATIONS"] = expirations

    def test_stops_one_day_before_expiry(self):
        self.configure("primary", "fallback", "primary=2026-09-09,fallback=2026-10-01")
        model, expiry = agent.select_model(date(2026, 9, 7))
        self.assertEqual((model, expiry), ("primary", date(2026, 9, 9)))

        model, expiry = agent.select_model(date(2026, 9, 8))
        self.assertEqual((model, expiry), ("fallback", date(2026, 10, 1)))

    def test_expired_primary_moves_to_unexpired_fallback(self):
        self.configure("primary", "fallback", "primary=2026-09-08,fallback=2026-10-01")
        model, expiry = agent.select_model(date(2026, 9, 9))
        self.assertEqual((model, expiry), ("fallback", date(2026, 10, 1)))
        self.assertIn("earliest_expiration", agent.MODEL_SELECTION_DETAIL)

    def test_earliest_expiration_wins_over_primary_order(self):
        self.configure("primary", "fallback", "primary=2026-12-01,fallback=2026-10-01")
        model, expiry = agent.select_model(date(2026, 9, 9))
        self.assertEqual((model, expiry), ("fallback", date(2026, 10, 1)))

    def test_missing_expiry_metadata_blocks_instead_of_guessing(self):
        self.configure("primary", "fallback", "primary=2026-10-01")
        with self.assertRaisesRegex(RuntimeError, "metadata missing"):
            agent.select_model(date(2026, 9, 9))

    def test_all_expired_models_block(self):
        self.configure("primary", "fallback", "primary=2026-09-08,fallback=2026-09-08")
        with self.assertRaisesRegex(RuntimeError, "past cutoff=primary,fallback"):
            agent.select_model(date(2026, 9, 9))

    def test_probe_rejected_model_is_skipped(self):
        """额度耗尽只有真打过去才知道，纸面规则拦不住 —— 证伪一个要能换下一个。

        实测撞到过：主模型免费额度用光，`select_model` 照样选它（没到期），
        然后第一次真实对话 HTTP 403 不可重试，整轮死掉。
        """
        self.configure("primary", "fallback", "primary=2026-10-01,fallback=2026-12-01")
        self.assertEqual(agent.select_model(date(2026, 9, 9))[0], "primary")
        self.assertEqual(
            agent.select_model(date(2026, 9, 9), skip={"primary"})[0], "fallback")

    def test_all_models_probe_rejected_blocks(self):
        """全被证伪就必须 blocked —— 不能绕回去用一个已经 403 的模型。"""
        self.configure("primary", "fallback", "primary=2026-10-01,fallback=2026-12-01")
        with self.assertRaisesRegex(RuntimeError, "probe-rejected=fallback,primary"):
            agent.select_model(date(2026, 9, 9), skip={"primary", "fallback"})


if __name__ == "__main__":
    unittest.main()
