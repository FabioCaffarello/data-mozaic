# Scout tool-host requirements

This skill intentionally does not invent a tool API. A production host must expose a controlled Scout capability and demonstrate, through executable configuration and tests:

- Identity comes from the host's authenticated caller; source revision, authorization receipt, and approved path/origin associations are resolved by policy services.
- All outbound requests, DNS resolutions, redirects, and subrequests pass through policy mediation. Private, loopback, link-local, reserved, and rebinding targets are blocked at the connection boundary; deployment egress controls provide defense in depth.
- Request, page, response-byte, and elapsed-time budgets are enforced and durable. Credential values are handled by a secret provider and are never copied into prompts, evidence, URLs, or traces.
- The tool process has no shell or network bypass available to the agent. Tool identity, trace, and evidence ports are least-privileged and owned by their respective bounded contexts.
- Captured response bodies have integrity digests and durable evidence references. Replays consume captured bytes only.

The repository's `scout.tools.HTTPDiscoveryTool` is an internal experimental adapter. It has no MCP/tool host, process sandbox, production BC06 evidence/trace adapter, or host identity integration. Passing a prompt or policy check does not constrain another process.
