# Resin Health Pool Guard

This sidecar maintains a fixed-size pool of healthy Resin sticky usernames for
Grok Build. It no longer generates SVGs and does not contain an image
classifier or model snapshot.

Candidate leases are tested with the same fixed prompt and streaming timing
method as Grok2API's upstream Egress Quality Guard:

```text
Write exactly 16 numbered lines about reliable distributed systems. Each line
must be one complete English sentence, with no markdown heading. The final line
must end with the exact marker QUALITY_OK.
```

A candidate is admitted only when:

- the response is a real SSE generation with a measurable generation window;
- the output contains `QUALITY_OK`;
- at least 32 output tokens are reported;
- `output_tokens / (duration - first_token) <= 200 tokens/s`;
- Cloudflare trace returns the actual public exit IP.

The public IP is stored with the Resin username. Bad IPs are quarantined for 24
hours across usernames, so generating another username that reaches the same
exit does not bypass the blacklist. The pool and blacklist are persisted by
Grok2API in PostgreSQL.

Production Build streams use the same 200 tokens/s threshold. Two consecutive
fast successful streams evict the exact Resin username used by those requests.
The sidecar then explores a replacement until the configured pool target is
restored.

The sidecar uses these administrator endpoints:

- `GET /api/admin/v1/pelican-egress-pool`
- `GET /api/admin/v1/pelican-egress-pool/bad`
- `POST /api/admin/v1/pelican-egress-pool/results`
- `POST /api/admin/v1/quality-tests/requests`

Credentials are read only from mounted deployment files and are never stored in
the repository or emitted in logs.
