from __future__ import annotations

import hashlib
import unittest

from acquisition_application.live_evidence import _plan_pagination_urls
from acquisition_application.plan_validation import verify_plan_results
from acquisition_application.result_bundle import (
    FixtureImportError,
    VerifiedFixturePage,
)


class AcquisitionPlanValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = {
            "source_id": "controlled-http",
            "revision": "controlled-v1",
            "origin": "https://api.fixture.test",
            "path_prefixes": ["/v1/"],
        }
        self.url = "https://api.fixture.test/v1/items"
        self.plan = {
            "profile": "scrapy-live-http-v1",
            "source": self.source,
            "fetch": {"inputs": {"url": self.url}},
            "budgets": {"max_records": 10, "max_output_bytes": 100_000, "max_pages": 1},
            "select": {
                "operator_type": "select.html_records",
                "inputs": {
                    "selector": "article.product",
                    "cardinality": "many",
                    "max_matches": 10,
                },
            },
            "extract": {
                "operation_id": "extract",
                "inputs": {
                    "fields": {
                        "id": {
                            "source": "attribute",
                            "attribute": "data-id",
                            "type": "string",
                            "required": True,
                            "normalize": ["trim"],
                        },
                        "title": {
                            "source": "text",
                            "selector": ".title",
                            "type": "string",
                            "required": True,
                            "normalize": ["trim"],
                        },
                        "price": {
                            "source": "text",
                            "selector": ".price",
                            "type": "decimal",
                            "required": True,
                            "normalize": ["trim"],
                        },
                    }
                },
            },
        }
        self.body = b'<article class="product" data-id=" sku-1 "><h2 class="title"> Mug </h2><span class="price">12.50</span></article>'
        self.digest = hashlib.sha256(self.body).hexdigest()
        self.page = VerifiedFixturePage(
            self.url,
            "fetch-1",
            "sha256:" + self.digest,
            "text/html",
            self.body,
        )
        self.record = {
            "id": "sku-1",
            "title": "Mug",
            "price": "12.5",
            "_provenance": {
                "source_id": "controlled-http",
                "source_revision": "controlled-v1",
                "source_url": self.url,
                "requested_url": self.url,
                "effective_url": self.url,
                "evidence_ref": "fetch-1",
                "raw_sha256": self.digest,
                "operation_id": "extract",
                "page_index": 0,
                "record_index": 0,
            },
        }

    def test_result_is_recomputed_from_compiled_plan_and_trusted_body(self):
        verify_plan_results(
            plan=self.plan,
            pages=(self.page,),
            records=(self.record,),
            source_id="controlled-http",
            source_revision="controlled-v1",
        )

    def test_worker_cannot_replace_extracted_value_or_source_binding(self):
        changed = {**self.record, "title": "Forged"}
        with self.assertRaises(FixtureImportError):
            verify_plan_results(
                plan=self.plan,
                pages=(self.page,),
                records=(changed,),
                source_id="controlled-http",
                source_revision="controlled-v1",
            )
        with self.assertRaises(FixtureImportError):
            verify_plan_results(
                plan=self.plan,
                pages=(self.page,),
                records=(self.record,),
                source_id="other-tenant-source",
                source_revision="controlled-v1",
            )

    def test_planned_pagination_is_recomputed_from_plan_and_page_bodies(self):
        first = VerifiedFixturePage(
            self.url,
            "fetch-1",
            "sha256:" + "1" * 64,
            "text/html",
            b'<a class="next" href="/v1/pages/2">Next</a>',
        )
        second_url = "https://api.fixture.test/v1/pages/2"
        second = VerifiedFixturePage(
            second_url,
            "fetch-2",
            "sha256:" + "2" * 64,
            "text/html",
            b'<a class="next" href="/v1/pages/3">Next</a>',
        )
        plan = {
            **self.plan,
            "discover": {"inputs": {"selector": "a.next", "max_links": 1}},
            "pagination": {
                "inputs": {"max_pages": 2, "completion_mode": "bounded_window"}
            },
        }
        _plan_pagination_urls(plan, [first, second], "planned_page_limit")
        with self.assertRaises(FixtureImportError):
            _plan_pagination_urls(plan, [first], "planned_page_limit")

    def test_natural_pagination_requires_evidence_of_natural_end(self):
        first = VerifiedFixturePage(
            self.url,
            "fetch-1",
            "sha256:" + "1" * 64,
            "text/html",
            b'<a class="next" href="/v1/pages/2">Next</a>',
        )
        second_url = "https://api.fixture.test/v1/pages/2"
        second = VerifiedFixturePage(
            second_url,
            "fetch-2",
            "sha256:" + "2" * 64,
            "text/html",
            b"<main>End</main>",
        )
        plan = {
            **self.plan,
            "discover": {"inputs": {"selector": "a.next", "max_links": 1}},
            "pagination": {
                "inputs": {"max_pages": 3, "completion_mode": "natural_end"}
            },
        }
        _plan_pagination_urls(plan, [first, second], "natural_end")
        with self.assertRaises(FixtureImportError):
            _plan_pagination_urls(plan, [first], "natural_end")


if __name__ == "__main__":
    unittest.main()
