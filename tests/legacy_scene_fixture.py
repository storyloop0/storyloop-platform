"""Serialized Harness v1 scene evidence for upgrade and migration tests."""

import hashlib
import json

from storyloop_harness.advanced import GeneratedSceneSnapshot


def legacy_scene_record(saved: GeneratedSceneSnapshot) -> GeneratedSceneSnapshot:
    """Encode the pre-beats prose/replies record with v1 identity and checksum."""
    body = saved.metadata()
    scene = body["scene"].copy()
    scene.pop("beats")
    scene["prose"] = "The dockhand sets down a rope."
    scene["replies"] = [{"actor_id": "dockhand", "speech": "The morning boat is due soon."}]
    body["scene"] = scene
    body["generation_contract_version"] = 1
    body.pop("snapshot_id")

    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)

    body["snapshot_id"] = hashlib.sha256(canonical(body).encode()).hexdigest()
    encoded = canonical(body)
    return GeneratedSceneSnapshot.from_storage(encoded, hashlib.sha256(encoded.encode()).hexdigest())
