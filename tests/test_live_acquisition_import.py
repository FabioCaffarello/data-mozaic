from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tooling"))

from fixture_acquisition import FixtureImportError, VerifiedFixturePage
from live_acquisition_import import (
    _controlled_next_links,
    _controlled_record_values,
    _validate_pagination_completion,
)


class ControlledLivePayloadTests(unittest.TestCase):
    def test_html_payload_maps_only_declared_product_fields(self):
        records = _controlled_record_values(
            b'<article class="product" data-id=" sku-1 "><h2 class="title"> Mug </h2><span class="price">12.5</span></article>',
            "text/html",
        )
        self.assertEqual([{"id": "sku-1", "title": "Mug", "price": "12.5"}], records)

    def test_html_payload_rejects_missing_required_fields(self):
        with self.assertRaises(FixtureImportError):
            _controlled_record_values(
                b'<article class="product" data-id="sku-1"><h2 class="title">Mug</h2></article>',
                "text/html",
            )

    def test_html_payload_rejects_unclosed_product(self):
        with self.assertRaises(FixtureImportError):
            _controlled_record_values(
                b'<article class="product" data-id="sku-1"><h2 class="title">Mug</h2><span class="price">12</span>',
                "text/html",
            )

    def test_unsupported_controlled_media_type_fails_closed(self):
        with self.assertRaises(FixtureImportError):
            _controlled_record_values(b"body", "application/octet-stream")

    def test_pagination_link_is_resolved_against_the_trusted_page(self):
        links = _controlled_next_links(
            b'<a class="next" href="/v1/pages/2">Next</a>',
            "https://api.fixture.test/v1/pages/1",
        )
        self.assertEqual(["https://api.fixture.test/v1/pages/2"], links)

    def test_duplicate_pagination_identity_is_rejected(self):
        with self.assertRaises(FixtureImportError):
            _controlled_next_links(
                b'<a class="next" href="/v1/pages/2">Next</a><a class="next" href="/v1/pages/2">Again</a>',
                "https://api.fixture.test/v1/pages/1",
            )

    def test_planned_window_accepts_only_explicit_full_page_limit(self):
        first = VerifiedFixturePage(
            "https://api.fixture.test/v1/window/1",
            "fetch-1",
            "sha256:" + "1" * 64,
            "text/html",
            b'<a class="next" href="/v1/window/2">Next</a>',
            None,
        )
        second = VerifiedFixturePage(
            "https://api.fixture.test/v1/window/2",
            "fetch-2",
            "sha256:" + "2" * 64,
            "text/html",
            b'<a class="next" href="/v1/window/3">Next</a>',
            None,
        )
        _validate_pagination_completion(
            [first, second],
            "planned_page_limit",
            authorized_mode="bounded_window",
            authorized_max_pages=2,
            authorized_origin="https://api.fixture.test",
            authorized_path_prefixes=("/v1/window/",),
        )

    def test_worker_cannot_claim_planned_limit_without_trusted_permission(self):
        page = VerifiedFixturePage(
            "https://api.fixture.test/v1/window/1",
            "fetch-1",
            "sha256:" + "1" * 64,
            "text/html",
            b'<a class="next" href="/v1/window/2">Next</a>',
            None,
        )
        with self.assertRaises(FixtureImportError):
            _validate_pagination_completion(
                [page],
                "planned_page_limit",
                authorized_mode="natural_end",
                authorized_max_pages=None,
                authorized_origin="https://api.fixture.test",
                authorized_path_prefixes=("/v1/window/",),
            )

    def test_planned_limit_rejects_wrong_page_count_and_out_of_scope_continuation(self):
        page = VerifiedFixturePage(
            "https://api.fixture.test/v1/window/1",
            "fetch-1",
            "sha256:" + "1" * 64,
            "text/html",
            b'<a class="next" href="https://other.test/v1/window/2">Next</a>',
            None,
        )
        with self.assertRaises(FixtureImportError):
            _validate_pagination_completion(
                [page],
                "planned_page_limit",
                authorized_mode="bounded_window",
                authorized_max_pages=2,
                authorized_origin="https://api.fixture.test",
                authorized_path_prefixes=("/v1/window/",),
            )


if __name__ == "__main__":
    unittest.main()
