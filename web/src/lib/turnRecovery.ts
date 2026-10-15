import type { TurnRecoveryStatus } from "./contracts";

export type RecoveryProof = {
  gameId: string;
  requestId: string;
  text: string;
  generation: number;
  status: TurnRecoveryStatus;
};

export function allowRecovery(status: TurnRecoveryStatus | null, sending: boolean): boolean {
  return !sending && status !== null && status.can_recover
    && (status.status === "recoverable" || status.status === "complete");
}

export function matchesRecovery(
  proof: RecoveryProof | null, gameId: string,
  pending: { id: string; text: string } | null, generation: number, sending: boolean,
): boolean {
  return !!proof && !!pending && !sending && proof.gameId === gameId
    && proof.requestId === pending.id && proof.text === pending.text
    && proof.generation === generation && proof.status.request_id === pending.id
    && allowRecovery(proof.status, sending);
}

export async function inputHash(text: string): Promise<string> {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function upsertCompletedTurn<T>(
  turns: { request_id: string; input: string | null; response: T }[],
  requestId: string, text: string, response: T,
): { request_id: string; input: string | null; response: T }[] {
  const next = turns.filter((turn) => turn.request_id !== requestId);
  const index = turns.findIndex((turn) => turn.request_id === requestId);
  next.splice(index < 0 ? next.length : index, 0, { request_id: requestId, input: text, response });
  return next;
}
