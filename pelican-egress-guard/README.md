# Pelican Egress Guard

This service maintains the Grok Build Resin good-username pool. It sends the
original prompt `画一个鹈鹕骑自行车的svg` through the admin quality-test API,
classifies the returned SVG using the fixed `pelican-knn-v1.json` snapshot, and
reports only `good` results with confidence at least `0.60` to Grok2API.

The snapshot is generated offline with `export_fixed_model.py`. Runtime does
not read credentials or retrain from annotations. Administrator credentials
are read from the mounted Grok2API config; no secrets belong in this folder.

Before every exploration or recheck, the guard resolves the candidate through
Resin and reads `https://cloudflare.com/cdn-cgi/trace`. The resulting public
`ip=` value is stored with the pool result and is the identity of the temporary
bad-egress blacklist. Consequently, two different Resin usernames that share
one public exit IP are quarantined together for 24 hours. A later `good`
result for that IP clears the quarantine. If Cloudflare trace cannot produce
an IP, the result can still remove a pool entry, but it does not create a
username-only blacklist entry.

The Grok2API admin endpoints used by the guard are:

- `GET /api/admin/v1/pelican-egress-pool`
- `GET /api/admin/v1/pelican-egress-pool/bad`
- `POST /api/admin/v1/pelican-egress-pool/results` with `exit_ip`

The `exit_ip` field is diagnostic and persistent on active pool rows; it is
not a credential and should not be replaced with the Resin username.
