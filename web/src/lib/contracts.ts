import validateView from "./generated/View.validator.js";
import validateHistory from "./generated/History.validator.js";
import validateFailure from "./generated/TurnFailure.validator.js";
import validateEvent from "./generated/TurnStreamEvent.validator.js";
import validateTurnRecoveryStatus from "./generated/TurnRecoveryStatus.validator.js";
import type { View } from "./generated/View";
import type { History } from "./generated/History";
import type { TurnFailure } from "./generated/TurnFailure";
import type { TurnStreamEvent } from "./generated/TurnStreamEvent";
import type { TurnRecoveryStatus } from "./generated/TurnRecoveryStatus";

export type { View, StorySegment, Interaction, TurnBilling } from "./generated/View";
export type { History } from "./generated/History";
export type { TurnFailure } from "./generated/TurnFailure";
export type { TurnStreamEvent } from "./generated/TurnStreamEvent";
export type { TurnRecoveryStatus } from "./generated/TurnRecoveryStatus";

export class ContractError extends Error {
  constructor() {
    super("未能确认服务器返回的完整结果。请使用原请求重试；若持续失败，请联系管理员。");
  }
}

export function parseView(value: unknown): View {
  if (!validateView(value)) throw new ContractError();
  return value;
}

export function parseTurnRecoveryStatus(value: unknown, requestId: string): TurnRecoveryStatus {
  if (!validateTurnRecoveryStatus(value) || (value as TurnRecoveryStatus).request_id !== requestId)
    throw new ContractError();
  return value as TurnRecoveryStatus;
}

export function parseHistory(value: unknown): History {
  if (!validateHistory(value)) throw new ContractError();
  return value;
}

export function parseTurnFailure(value: unknown): TurnFailure {
  if (!validateFailure(value)) throw new ContractError();
  return value;
}

export function parseTurnStreamEvent(value: unknown): TurnStreamEvent {
  if (!validateEvent(value)) throw new ContractError();
  return value;
}

export function parseStreamData(data: string): TurnStreamEvent {
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    throw new ContractError();
  }
  return parseTurnStreamEvent(value);
}
