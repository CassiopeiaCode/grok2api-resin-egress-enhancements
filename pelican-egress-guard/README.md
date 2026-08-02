# Pelican Egress Guard

This service maintains the Grok Build Resin good-username pool. It sends the
original prompt `画一个鹈鹕骑自行车的svg` through the admin quality-test API,
classifies the returned SVG using the fixed `pelican-knn-v1.json` snapshot, and
reports only `good` results with confidence at least `0.60` to Grok2API.

The snapshot is generated offline with `export_fixed_model.py`. Runtime does
not read credentials or retrain from annotations. Administrator credentials
are read from the mounted Grok2API config; no secrets belong in this folder.
