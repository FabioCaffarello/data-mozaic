from __future__ import annotations

import hashlib
import unittest

from acquisition_application.live_evidence import _authorized_parameterized_target
from acquisition_application.result_bundle import FixtureImportError


class AuthorizedParameterizedTargetTests(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "fetch": {
                "inputs": {
                    "url": "https://api.fixture.test/v1/items?limit=0",
                    "input_query": {"parameter": "limit", "query_name": "limit"},
                }
            }
        }

    def _target(self, payload: bytes) -> str:
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        return _authorized_parameterized_target(
            self.plan, payload.decode("utf-8"), digest
        )

    def test_uses_exact_bc04_value_and_component_encodes_it(self):
        self.assertEqual(
            "https://api.fixture.test/v1/items?limit=a%26admin%3Dtrue",
            self._target(b'{"limit":"a&admin=true"}'),
        )

    def test_rejects_altered_digest_and_duplicate_properties(self):
        with self.assertRaisesRegex(FixtureImportError, "digest"):
            _authorized_parameterized_target(
                self.plan, '{"limit":2}', "sha256:" + "0" * 64
            )
        with self.assertRaisesRegex(FixtureImportError, "invalid"):
            payload = b'{"limit":1,"limit":2}'
            _authorized_parameterized_target(
                self.plan,
                payload.decode(),
                "sha256:" + hashlib.sha256(payload).hexdigest(),
            )

    def test_rejects_extra_parameter_and_ambiguous_compiled_queries(self):
        with self.assertRaisesRegex(FixtureImportError, "compiled input"):
            self._target(b'{"limit":2,"other":"x"}')
        self.plan["fetch"]["inputs"]["url"] = (
            "https://api.fixture.test/v1/items?limit=0&limit=1"
        )
        payload = b'{"limit":2}'
        with self.assertRaisesRegex(FixtureImportError, "differs from its binding"):
            self._target(payload)

    def test_rejects_parameter_binding_on_nonparameterized_plan(self):
        self.plan["fetch"]["inputs"].pop("input_query")
        with self.assertRaisesRegex(FixtureImportError, "do not match"):
            self._target(b'{"limit":2}')


if __name__ == "__main__":
    unittest.main()
