# mozaic-bot-build

Owner: BC15 BotBuild contract. This internal, stdlib-only library verifies manifest identity, exact file membership, runtime file digests, and safe paths before executable BotBuild code is imported.

Consumers are BC15's Python Reference and Scrapy verifiers plus the Scrapy Collector assembly/launcher. It does not validate Playbook semantics, authorize execution, verify signatures, import Scrapy, or depend on a compiler checkout.

The consumer API is `verify_bot_build(root, expected_manifest_digest, supported_versions)`. Callers separately enforce backend, capability profile, execution mode, Python/Scrapy compatibility and authorization policy.

Run its isolated tests with:

```sh
uv run --locked --package mozaic-bot-build python -m unittest discover -s libs/python/bot-build/tests -v
```
