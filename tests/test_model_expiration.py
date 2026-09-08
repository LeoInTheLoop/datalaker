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

    def test_expiry_date_is_inclusive(self):
        self.configure("primary", "fallback", "primary=2026-09-09,fallback=2026-10-01")
        model, expiry = agent.select_model(date(2026, 9, 9))
        self.assertEqual((model, expiry), ("primary", date(2026, 9, 9)))

    def test_expired_primary_moves_to_unexpired_fallback(self):
        self.configure("primary", "fallback", "primary=2026-09-08,fallback=2026-10-01")
        model, expiry = agent.select_model(date(2026, 9, 9))
        self.assertEqual((model, expiry), ("fallback", date(2026, 10, 1)))
        self.assertIn("primary_expired=primary", agent.MODEL_SELECTION_DETAIL)

    def test_missing_expiry_metadata_blocks_instead_of_guessing(self):
        self.configure("primary", "fallback", "primary=2026-10-01")
        with self.assertRaisesRegex(RuntimeError, "metadata missing"):
            agent.select_model(date(2026, 9, 9))

    def test_all_expired_models_block(self):
        self.configure("primary", "fallback", "primary=2026-09-08,fallback=2026-09-08")
        with self.assertRaisesRegex(RuntimeError, "all configured models expired"):
            agent.select_model(date(2026, 9, 9))


if __name__ == "__main__":
    unittest.main()
