# Evidence and findings

Each finding must cite one or more durable evidence manifest IDs. Preserve the distinction between a claim and its support:

| Classification | Meaning |
| --- | --- |
| `observed` | Directly present in captured source evidence. |
| `hypothesized` | Interpretation or proposed strategy not yet independently checked. |
| `independently_verified` | Checked with a separate method and separately identified evidence. |

The current `scout_record_finding` v1 MCP contract accepts `observation` or `hypothesis`; it has no independent-verification method. Do not label a finding independently verified in this workflow. Transcript and draft projections use `observed`/`hypothesized` to preserve that distinction.

`scout_export_transcript` returns metadata only. For replay, `scout_export_replay_fixtures` reauthorizes the proposal/session and writes captured HTTP bytes to the configured artifact root after custody and digest checks. It returns only the fixture path and references. The fixture body is still external source data; keep it in the approved local artifact area and do not paste raw content into a review report.

The CapturePlaybook draft copies finding IDs, classifications, and evidence references. It does not change BC14 ownership of observations or Scout v1 contracts. A draft, successful fixture replay, or BC15 receipt does not approve a source policy or release a bot.
