# OpenCode prototype

This is an integration milestone, not the complete MVP. The coordinator can invoke real OpenCode HTTP sessions, but this environment has only tested it against a fake HTTP server. No compatible release has yet been pinned or accepted.

1. Install OpenCode locally and configure your model/provider using its own setup.
2. In an isolated test repository, merge `opencode/prototype.json` into `opencode.json`. Review the effective permissions: shell and compute tools are deliberately disabled in these initial profiles.
3. Start `opencode serve --hostname 127.0.0.1 --port 4096`. Configure `OPENCODE_SERVER_PASSWORD` in server and client environments.
4. Install ASSR with `python -m pip install -e .`.
5. Run `assr create sample developer`, then `assr step sample 'Inspect the repository and propose a small change. Do not edit yet.'`.
6. Prototype question path: `assr ask q1 sample 'Which approach?'`, then `assr answer q1 k2 1 'Use approach A'`.
7. Save a concrete plan with `assr plan sample 'Implement approach A in the specified file'`. Use the returned digest in `assr admit sample DIGEST k2`.
8. Run `assr step sample 'Execute the admitted plan'`. The adapter uses the same OpenCode session with the developer-build profile.
9. Inspect `assr events`.

The CLI actor argument is for trusted local testing, not authenticated agent identity. This is NOT autonomous supervisory routing: the plugin bridge and dispatcher remain to be implemented. Do not expose this interface as a network service.

Lost HTTP responses block the task instead of retrying potentially accepted writes. Reconciliation tooling is still outstanding; do not reset the database while external actions may be active.

Documentation used: https://opencode.ai/docs/server/ and https://opencode.ai/docs/agents/ . Exact server compatibility requires live validation.
