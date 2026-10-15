"""The offline corpus exercises real HTTP handlers; Node is a separate CI gate."""
from player_contract_corpus import export_corpus
from storyloop_platform.api.contracts import History, TurnFailure, View, stream_event_adapter, turn_recovery_status_adapter


def test_actual_handler_corpus_covers_modes_billing_and_failures(tmp_path):
    corpus = export_corpus(tmp_path)
    assert {item["kind"] for item in corpus} == {"View", "History", "TurnFailure", "TurnStreamEvent", "TurnRecoveryStatus"}
    parsers = {"View": View.model_validate, "History": History.model_validate,
               "TurnFailure": TurnFailure.model_validate, "TurnStreamEvent": stream_event_adapter.validate_python,
               "TurnRecoveryStatus": turn_recovery_status_adapter.validate_python}
    for item in corpus:
        parsers[item["kind"]](item["payload"])
    views = [item["payload"] for item in corpus if item["kind"] == "View"]
    assert {view["mode"] for view in views} == {"campaign", "freeform"}
    assert {view["presentation_mode"] for view in views} == {"novel", "interactive"}
    assert any(view["billing"]["model_calls"] > 0 for view in views if "billing" in view)
    assert any(view["complete"] and view["interaction"] is None for view in views)
    assert any(view["interaction"] is not None for view in views)
    intro = next(item["payload"] for item in corpus if item["name"] == "create:freeform:default")
    assert [field["value"] for field in intro["status_fields"]] == [False, 0, "0"]
    assert intro["status_fields"][1]["min"] == 0 and intro["status_fields"][1]["max"] == 10
    assert {item["payload"]["code"] for item in corpus if item["kind"] == "TurnFailure"} >= {
        "INVALID_TURN_INPUT", "TURN_RECOVERY_REQUIRED", "MODEL_UNAVAILABLE", "SERVICE_STOPPING",
        "GENERATION_UNCERTAIN"}
    assert {item["payload"]["type"] for item in corpus if item["kind"] == "TurnStreamEvent"} == {
        "stage", "segment", "preview", "complete", "error"}
    assert {item["payload"]["reason_code"] for item in corpus if item["kind"] == "TurnRecoveryStatus"} == {
        "RECEIPT_READY", "INPUT_MISMATCH", "NO_SNAPSHOT", "LEGACY_REQUEST", "GENERATION_UNCERTAIN"}
    uncertain = [item for item in corpus if item["name"].startswith("failure:uncertain:")
                 and item["payload"].get("code") == "GENERATION_UNCERTAIN"]
    assert len(uncertain) == 2
    assert all(item["payload"]["request_id"] == "native-uncertain"
               and item["payload"]["retryable"] is False
               and item["payload"]["commit_state"] == "unknown" for item in uncertain)
    assert all(item["payload"]["message"] ==
               "这条行动的生成结果暂时无法确认，已停止重试。请保留此请求并联系管理员。" for item in uncertain)
    assert all(item["payload"] == {"request_id": "native-uncertain", "status": "blocked",
                                   "can_recover": False, "reason_code": "GENERATION_UNCERTAIN"}
               for item in corpus if item["name"].startswith("status:uncertain:"))
