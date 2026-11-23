# Codex fixture authorship demonstration

This local conversation authored `capture-playbook.yaml` after creating a new synthetic fixture and independent expected records. No Scout session, remote source, browser, or HTTP request was used. The page's instruction-like text was returned only as a bounded sample marked `untrusted_content: true`; it did not change the objective or cause a shell/network operation.

## Objective and evidence

Objective: extract publication code, title, year, and price. Fixture evidence is `e-auth-demo-1`, SHA-256 `d803241b53874b42fc39ddf49e4868a78ee004a308652505f6aca5a4f4b5d7fe`, 534 bytes.

The Codex session ran:

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering inspect-fixture examples/playbooks/codex-authorship-demo/fixtures https://authoring.fixture.test/library/
uv run --locked --package mozaic-collector-engineering python -m collector_engineering try-selector examples/playbooks/codex-authorship-demo/fixtures https://authoring.fixture.test/library/ 'li.publication' --evidence-ref e-auth-demo-1
uv run --locked --package mozaic-collector-engineering python -m collector_engineering try-selector examples/playbooks/codex-authorship-demo/fixtures https://authoring.fixture.test/library/ '.title' --evidence-ref e-auth-demo-1 --max-samples 2
uv run --locked --package mozaic-collector-engineering python -m collector_engineering try-selector examples/playbooks/codex-authorship-demo/fixtures https://authoring.fixture.test/library/ '.year' --evidence-ref e-auth-demo-1 --max-samples 2
uv run --locked --package mozaic-collector-engineering python -m collector_engineering try-selector examples/playbooks/codex-authorship-demo/fixtures https://authoring.fixture.test/library/ '.price' --evidence-ref e-auth-demo-1 --max-samples 2
```

The tools reported two `li.publication` matches and two matches for each field selector. Samples were bounded and linked to the same evidence digest. The tool explicitly returned `semantic_correctness_proven: false`.

## Validation correction

The first authored candidate used `article.publication`. Validation returned:

```text
SELECTOR_NO_MATCHES $.operations[1].selector: selector has no matches in its linked evidence fixtures
```

Codex compared the diagnostic with the inspected structure and tested `li.publication`, then corrected the playbook. Validation with fixtures passed after the edit.

## Build and independent acceptance

Commands run:

```sh
uv run --locked --package mozaic-collector-engineering python -m collector_engineering validate examples/playbooks/codex-authorship-demo/capture-playbook.yaml --fixtures examples/playbooks/codex-authorship-demo/fixtures
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compile examples/playbooks/codex-authorship-demo/capture-playbook.yaml --backend python-http --fixtures examples/playbooks/codex-authorship-demo/fixtures --output /private/tmp/dm-codex-authored-build
uv run --locked --package mozaic-collector-engineering python -m collector_engineering verify-build /private/tmp/dm-codex-authored-build
sandbox-exec -p '(version 1)(allow default)(deny network*)' .venv/bin/python /private/tmp/dm-codex-authored-build/bot.py --fixtures "$PWD/examples/playbooks/codex-authorship-demo/fixtures" --output /private/tmp/dm-codex-authored-output
uv run --locked --package mozaic-collector-engineering python -m collector_engineering compare examples/playbooks/codex-authorship-demo/expected-records.jsonl /private/tmp/dm-codex-authored-output/records.jsonl
```

Results: `verify-build` passed with build ID `dee153c3ec85b8d76f83db8063c54fac41c9171525b81ce8764faf41938b23d9`; the generated bot ran with macOS network access denied by `sandbox-exec`; it emitted two records and one request/page; independent byte comparison passed for both expected records. The final `make collector-playbook-examples` rerun reproduced that build ID and passed the independent comparison again.

This demonstrates local synthetic-fixture authorship in the Codex development session. It is distinct from automated compiler tests and does not prove operational authorization for remote sources. PR #9 Scout deployment gates remain outside this demonstration and are still required for any real source session.
