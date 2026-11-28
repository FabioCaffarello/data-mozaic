import unittest

from acquisition_application.adapters.evidence_release_authorization import (
    EvidenceReleaseOperatorAllowlist,
)
from acquisition_application.application import TrustedAcquisitionApplication
from acquisition_application.context import AcquisitionApplicationError


class EvidenceReleaseAuthorizationTests(unittest.TestCase):
    def test_authorizes_configured_operator_for_exact_scope(self):
        allowlist = EvidenceReleaseOperatorAllowlist(["ops-a", " ops-b "])
        allowlist.assert_can_release("ops-a", "tenant-a", "run-a")

    def test_rejects_unconfigured_operator_and_missing_scope(self):
        allowlist = EvidenceReleaseOperatorAllowlist(["ops-a"])
        for identity, tenant, run in (
            ("ops-c", "tenant-a", "run-a"),
            ("ops-a", "", "run-a"),
            ("ops-a", "tenant-a", ""),
        ):
            with (
                self.subTest(identity=identity, tenant=tenant, run=run),
                self.assertRaises(PermissionError),
            ):
                allowlist.assert_can_release(identity, tenant, run)

    def test_release_fails_closed_without_an_authorization_port(self):
        application = TrustedAcquisitionApplication(
            object(), object(), object(), evidence_release=object()
        )
        with self.assertRaisesRegex(
            AcquisitionApplicationError,
            "operator authorization is not configured",
        ):
            application.release_published_evidence(
                "tenant-a", "run-a", object(), operator_identity="ops-a"
            )


if __name__ == "__main__":
    unittest.main()
