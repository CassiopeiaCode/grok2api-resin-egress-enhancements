#!/usr/bin/env python3
"""Export the current annotations as a reproducible fixed KNN snapshot."""
import hashlib, json, sys
from pathlib import Path
from guard import KNNClassifier, ANNOTATIONS_PATH

out = Path(sys.argv[1] if len(sys.argv) > 1 else "model/pelican-knn-v1.json")
classifier = KNNClassifier.from_annotations()
payload = {
    "model_version": "pelican-knn-v1",
    "feature_version": "svg-features-46-v1",
    "k": 5,
    "distance_epsilon": 0.15,
    "unknown_distance": 8.0,
    "minimum_weight": 0.42,
    "annotation_source_sha256": hashlib.sha256(ANNOTATIONS_PATH.read_bytes()).hexdigest(),
    "sample_count": len(classifier.vectors),
    "feature_count": len(classifier.means),
    "means": classifier.means,
    "scales": classifier.scales,
    "vectors": classifier.vectors,
    "labels": classifier.labels,
}
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"exported {len(classifier.vectors)} samples/{len(classifier.means)} features to {out}")
