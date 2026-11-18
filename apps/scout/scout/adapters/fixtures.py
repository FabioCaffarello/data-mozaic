"""Server-owned, hermetic HTML fixtures for Scout application testing."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from ..domain import AuthorizationError, IntegrityError, ScoutScope, SourceRef
from ..ports import FixtureExplorationResult, FixtureLink
from ..tools.url_policy import TargetRejected, normalize_url

_FIXTURES = {
    "catalog-home": (
        SourceRef("catalog-test", "revision-1"),
        "https://catalog.test/",
        "catalog-home.html",
    ),
    "catalog-page-2": (
        SourceRef("catalog-test", "revision-1"),
        "https://catalog.test/items/page-2",
        "catalog-page-2.html",
    ),
}
_FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "mcp-catalog"


class _HTMLSummary(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self._title_depth = 0
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "a" and self._skip_depth == 0:
            href = dict(attrs).get("href")
            if href and len(self.links) < 100:
                self.links.append(href)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth = max(0, self._skip_depth - 1)
        if tag == "title":
            self._title_depth = max(0, self._title_depth - 1)

    def handle_data(self, data):
        if self._skip_depth:
            return
        value = " ".join(data.split())
        if value:
            self.text.append(value)
            if self._title_depth:
                self.title.append(value)


class FixtureExplorer:
    """Read only named package fixtures; this adapter never opens a socket."""

    def explore(
        self, source: SourceRef, scope: ScoutScope, fixture_id: str, max_bytes: int
    ) -> FixtureExplorationResult:
        entry = _FIXTURES.get(fixture_id)
        if entry is None or entry[0] != source:
            raise AuthorizationError("fixture is not registered for source revision")
        target = normalize_url(entry[1])
        if not scope.allows(target.origin, target.path, "GET"):
            raise AuthorizationError("fixture target is outside authorized scope")
        path = _FIXTURE_ROOT / entry[2]
        if path.is_symlink() or _FIXTURE_ROOT.is_symlink():
            raise IntegrityError("fixture path cannot be a symlink")
        if _FIXTURE_ROOT.resolve() not in path.resolve().parents:
            raise IntegrityError("fixture path escaped registered catalog")
        try:
            with path.open("rb") as stream:
                body = stream.read(min(max_bytes, 2_000_000) + 1)
        except OSError as error:
            raise IntegrityError("registered fixture is unavailable") from error
        if len(body) > min(max_bytes, 2_000_000):
            raise IntegrityError("registered fixture exceeds response byte budget")
        parser = _HTMLSummary()
        parser.feed(body.decode("utf-8", errors="replace"))
        links = []
        for raw_link in parser.links:
            try:
                normalized = normalize_url(raw_link)
            except (TargetRejected, ValueError):
                continue
            if scope.allows(normalized.origin, normalized.path, "GET"):
                links.append(
                    FixtureLink(normalized.url, normalized.origin, normalized.path)
                )
        return FixtureExplorationResult(
            fixture_id,
            target.url,
            target.origin,
            target.path,
            200,
            "text/html",
            body,
            " ".join(parser.title)[:500] or None,
            " ".join(parser.text)[:16_000],
            tuple(links),
        )
