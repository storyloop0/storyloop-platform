/* Generated from api/contracts.py. Run npm run contracts:generate. */

export type TurnRecoveryStatus =
  | CompleteTurnRecoveryStatus
  | RecoverableTurnRecoveryStatus
  | BlockedTurnRecoveryStatus
  | UnavailableTurnRecoveryStatus;
export type CanRecover = true;
export type ReasonCode = "RECEIPT_READY";
export type RequestId = string;
export type Status = "complete";
export type CanRecover1 = true;
export type ReasonCode1 = "PREPARED_READY" | "SCENE_READY";
export type RequestId1 = string;
export type Status1 = "recoverable";
export type CanRecover2 = false;
export type ReasonCode2 =
  | "INPUT_MISMATCH"
  | "LATER_REQUEST"
  | "EVIDENCE_INVALID"
  | "CONFIG_CHANGED"
  | "USAGE_UNAVAILABLE"
  | "LEGACY_REQUEST"
  | "GENERATION_UNCERTAIN";
export type RequestId2 = string;
export type Status2 = "blocked";
export type CanRecover3 = false;
export type ReasonCode3 = "NO_SNAPSHOT" | "UNSUPPORTED";
export type RequestId3 = string;
export type Status3 = "not_available";

export interface CompleteTurnRecoveryStatus {
  can_recover: CanRecover;
  reason_code: ReasonCode;
  request_id: RequestId;
  status: Status;
}
export interface RecoverableTurnRecoveryStatus {
  can_recover: CanRecover1;
  reason_code: ReasonCode1;
  request_id: RequestId1;
  status: Status1;
}
export interface BlockedTurnRecoveryStatus {
  can_recover: CanRecover2;
  reason_code: ReasonCode2;
  request_id: RequestId2;
  status: Status2;
}
export interface UnavailableTurnRecoveryStatus {
  can_recover: CanRecover3;
  reason_code: ReasonCode3;
  request_id: RequestId3;
  status: Status3;
}
