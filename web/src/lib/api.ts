import { parseView, parseHistory } from "./contracts";
import type { View, TurnStreamEvent } from "./contracts";
import { ApiError, responseError, readTurnStream } from "./playerResponses";
export { ApiError } from "./playerResponses";
export type { View, History, StorySegment, Interaction, TurnBilling, TurnFailure, TurnStreamEvent } from "./contracts";

const API_BASE = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

export type Game = {
  id: string;
  title: string;
  mode: "campaign" | "freeform";
  play_modes?: ("campaign" | "freeform")[];
  summary: string;
  genre: string;
  theme: string;
  cover_url?: string | null;
  story_setup?: {
    player_options: { id: string; label: string }[];
    tone_options: { id: string; label: string }[];
    custom_fields: { id: string; label: string; required: boolean; max_length: number }[];
  } | null;
};
export type CastMember = { id: string; name: string; portrait_url: string | null };
export type CharacterDetail = CastMember & {
  profile: string;
  affinity: number | null;
  shared_event_count: number;
  memories: string[];
};
export type PlayerCard = {
  name: string;
  fields: { key: string; value: string }[];
};
export type UserScenario = {
  id: string;
  title: string;
  summary: string;
  mode: "campaign" | "freeform";
  status: "draft" | "published";
  package_version: string;
  version_id: string;
  review_status: "pending" | "approved" | "rejected" | "withdrawn" | null;
  submission_id: string | null;
  review_reason: string | null;
  public_state: "active" | "retired" | "blocked" | null;
  created_at: number;
};
export type ReviewSubmission = {
  submission_id: string;
  scenario_id: string;
  version_id: string;
  author_id: string;
  author_name?: string;
  title: string;
  summary: string;
  mode: "campaign" | "freeform";
  package_version: string;
  package_hash: string;
  status: "pending" | "approved" | "rejected" | "withdrawn";
  submitted_at: number;
  decided_at: number | null;
  reviewer_id: string | null;
  reason: string | null;
};
export type ReviewDetail = ReviewSubmission & {
  manifest: Record<string, unknown>;
  worldbook: Record<string, unknown>;
  campaign: Record<string, unknown> | null;
  story_blueprint?: Record<string, unknown> | null;
};
export type ManagedUser = {
  player_id: string;
  username: string;
  status: "active" | "suspended";
  roles: string[];
  created_at: number;
};
export type ManagedInvitation = {
  id: string;
  issuer_name: string | null;
  issued_by: string | null;
  used_name: string | null;
  created_at: number | null;
  expires_at: number;
  used_at: number | null;
  revoked_at: number | null;
  status: "available" | "used" | "expired" | "revoked";
};
export type PublicRelease = {
  scenario_id: string;
  version_id: string;
  submission_id: string;
  title: string;
  summary: string;
  state: "active" | "retired" | "blocked";
  updated_at: number;
};
export type AuditEvent = {
  event_id: string;
  actor_id: string;
  action: string;
  target_type: string;
  target_id: string;
  details: Record<string, unknown>;
  created_at: number;
};
export type Save = {
  game_id: string;
  catalog_id: string;
  title: string;
  mode: string | null;
  available: boolean;
  unavailable_reason?: string | null;
  tick: number;
  day: number | null;
  complete: boolean;
};
export type SaveSettings = {
  temperature: number;
  context_window_tokens: number;
  default_temperature: number;
  default_context_window_tokens: number;
  max_context_window_tokens: number;
};
export type CreditWallet = {
  balance_milli_points: number;
  balance_points: string;
  welcome_points: number;
  points_per_rmb: number;
};
export type CreditEntry = {
  entry_id: string;
  kind: "welcome" | "turn";
  game_id: string | null;
  request_id: string | null;
  delta_milli_points: number;
  delta_points: string;
  usage_cost_milli_points: number;
  balance_after_points: string;
  pricing_version: string;
  usage: {
    model: string;
    task: string;
    input_tokens: number;
    output_tokens: number;
    cached_input_tokens: number;
  }[];
  created_order: number;
};
export type Session = { player_id: string; roles: string[]; capabilities: string[] };
export type PlayerMemoryStatus = {
  available: boolean;
  enabled: boolean;
  memories: { id: string; text: string; created_at: string; updated_at: string; source: string }[];
  queued_inputs: number;
  batch_size: number;
  min_interval_hours: number;
  charged_points: number;
};
async function requestPayload(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<unknown> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("story:session-expired"));
    throw responseError(response.status, payload);
  }
  return payload;
}

async function request<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  return await requestPayload(path, method, body) as T;
}

async function requestContract<T>(path: string, parse: (payload: unknown) => T,
  method = "GET", body?: unknown): Promise<T> {
  return parse(await requestPayload(path, method, body));
}

async function uploadScenario(
  title: string,
  summary: string,
  file: File,
): Promise<UserScenario> {
  const form = new FormData();
  form.append("title", title);
  form.append("summary", summary);
  form.append("file", file);
  const response = await fetch(`${API_BASE}/v1/my-scenarios`, {
    method: "POST",
    credentials: "same-origin",
    body: form,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("story:session-expired"));
    throw new ApiError(
      payload && typeof payload.detail === "string"
        ? payload.detail : "上传未能完成，请稍后重试。",
      response.status,
    );
  }
  return payload as UserScenario;
}

async function uploadScenarioVersion(
  id: string, title: string, summary: string, file: File,
): Promise<UserScenario> {
  const form = new FormData();
  form.append("title", title);
  form.append("summary", summary);
  form.append("file", file);
  const response = await fetch(`${API_BASE}/v1/my-scenarios/${encodeURIComponent(id)}/versions`, {
    method: "POST", credentials: "same-origin", body: form,
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new Event("story:session-expired"));
    throw new ApiError(payload && typeof payload.detail === "string"
      ? payload.detail : "版本上传未能完成。", response.status);
  }
  return payload as UserScenario;
}

async function streamTurn(
  gameId: string,
  text: string,
  requestId: string,
  onEvent: (event: TurnStreamEvent) => void,
): Promise<View> {
  const response = await fetch(
    `${API_BASE}/v1/saves/${encodeURIComponent(gameId)}/turns/stream`,
    {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ text, request_id: requestId }),
    },
  );
  if (response.status === 401) window.dispatchEvent(new Event("story:session-expired"));
  return readTurnStream(response, onEvent);
}

export const api = {
  register: (username: string, password: string, inviteCode: string) =>
    request<Session>("/v1/accounts", "POST", {
      username, password, invite_code: inviteCode || undefined,
    }),
  login: (username: string, password: string) =>
    request<Session>("/v1/sessions", "POST", { username, password }),
  currentSession: () => request<Session>("/v1/sessions/current"),
  playerMemory: () => request<PlayerMemoryStatus>("/v1/me/memory"),
  setPlayerMemory: (enabled: boolean) =>
    request<PlayerMemoryStatus>("/v1/me/memory/settings", "POST", { enabled }),
  clearPlayerMemory: () => request<PlayerMemoryStatus>("/v1/me/memory", "DELETE"),
  correctPlayerMemory: (id: string, text: string) =>
    request<PlayerMemoryStatus>(`/v1/me/memory/${encodeURIComponent(id)}`, "PATCH", { text }),
  deletePlayerMemory: (id: string) =>
    request<PlayerMemoryStatus>(`/v1/me/memory/${encodeURIComponent(id)}`, "DELETE"),
  logout: () => request<{ status: string }>("/v1/sessions/current", "DELETE"),
  catalog: () => request<{ games: Game[] }>("/v1/catalog"),
  myScenarios: () => request<{ scenarios: UserScenario[] }>("/v1/my-scenarios"),
  mySubmissions: () => request<{ submissions: ReviewSubmission[] }>("/v1/my-submissions"),
  uploadScenario,
  uploadScenarioVersion,
  publishScenario: (id: string) =>
    request<UserScenario>(`/v1/my-scenarios/${encodeURIComponent(id)}/publish`, "POST"),
  submitScenario: (id: string) =>
    request<ReviewSubmission>(`/v1/my-scenarios/${encodeURIComponent(id)}/submit`, "POST"),
  withdrawSubmission: (id: string) =>
    request<ReviewSubmission>(`/v1/my-submissions/${encodeURIComponent(id)}`, "DELETE"),
  reviewQueue: (status = "pending") =>
    request<{ submissions: ReviewSubmission[] }>(`/v1/manage/submissions?status=${encodeURIComponent(status)}`),
  reviewDetail: (id: string) =>
    request<ReviewDetail>(`/v1/manage/submissions/${encodeURIComponent(id)}`),
  reviewPreview: (id: string) =>
    requestContract(`/v1/manage/submissions/${encodeURIComponent(id)}/preview`, parseView, "POST"),
  reviewDecide: (id: string, decision: "approved" | "rejected", reason: string) =>
    request<ReviewSubmission>(`/v1/manage/submissions/${encodeURIComponent(id)}/decision`,
      "POST", { decision, reason }),
  managedUsers: () => request<{ users: ManagedUser[] }>("/v1/manage/users"),
  managedInvitations: () => request<{ invites: ManagedInvitation[] }>("/v1/manage/invites"),
  issueInvitations: (count: number) =>
    request<{ codes: string[]; expires_at: number }>("/v1/manage/invites", "POST", { count }),
  revokeInvitation: (id: string) =>
    request<{ id: string; status: "revoked"; revoked_at: number }>(
      `/v1/manage/invites/${encodeURIComponent(id)}/revoke`, "POST"),
  setRole: (id: string, role: "reviewer" | "admin", enabled: boolean) =>
    request<{ player_id: string; roles: string[] }>(`/v1/manage/users/${encodeURIComponent(id)}/role`,
      "POST", { role, enabled }),
  setAccountStatus: (id: string, status: "active" | "suspended") =>
    request<{ player_id: string; status: string }>(`/v1/manage/users/${encodeURIComponent(id)}/status`,
      "POST", { status }),
  publicReleases: () => request<{ releases: PublicRelease[] }>("/v1/manage/releases"),
  setReleaseState: (id: string, state: "active" | "retired" | "blocked", reason: string) =>
    request<{ scenario_id: string; state: string }>(`/v1/manage/releases/${encodeURIComponent(id)}/state`,
      "POST", { state, reason }),
  managementAudit: () => request<{ events: AuditEvent[] }>("/v1/manage/audit"),
  deleteScenarioDraft: (id: string) =>
    request<{ status: string }>(`/v1/my-scenarios/${encodeURIComponent(id)}`, "DELETE"),
  saves: () => request<{ saves: Save[] }>("/v1/saves"),
  wallet: () => request<CreditWallet>("/v1/billing/wallet"),
  creditLedger: () => request<{ entries: CreditEntry[] }>("/v1/billing/ledger"),
  createSave: (catalogId: string, playMode?: "campaign" | "freeform",
    storySetup?: Record<string, string>) =>
    requestContract("/v1/saves", parseView, "POST", {
      catalog_id: catalogId, play_mode: playMode, story_setup: storySetup,
    }),
  resume: (gameId: string) =>
    requestContract(
      `/v1/saves/${encodeURIComponent(gameId)}/resume`, parseView,
      "POST",
    ),
  history: (gameId: string) =>
    requestContract(`/v1/saves/${encodeURIComponent(gameId)}/history`, parseHistory),
  playerCard: (gameId: string) =>
    request<PlayerCard>(`/v1/saves/${encodeURIComponent(gameId)}/player-card`),
  cast: (gameId: string) =>
    request<{ cast: CastMember[] }>(`/v1/saves/${encodeURIComponent(gameId)}/cast`),
  characterDetail: (gameId: string, actorId: string) =>
    request<CharacterDetail>(`/v1/saves/${encodeURIComponent(gameId)}/cast/${encodeURIComponent(actorId)}`),
  saveSettings: (gameId: string) =>
    request<SaveSettings>(`/v1/saves/${encodeURIComponent(gameId)}/settings`),
  updateSaveSettings: (gameId: string, temperature: number, contextWindowTokens: number) =>
    request<SaveSettings>(`/v1/saves/${encodeURIComponent(gameId)}/settings`,
      "PUT", { temperature, context_window_tokens: contextWindowTokens }),
  turn: (gameId: string, text: string, requestId: string) =>
    requestContract(
      `/v1/saves/${encodeURIComponent(gameId)}/turns`, parseView,
      "POST",
      { text, request_id: requestId },
    ),
  turnStream: streamTurn,
};
