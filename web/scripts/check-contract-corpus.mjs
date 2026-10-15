import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { ContractError, parseView, parseHistory, parseTurnFailure, parseTurnRecoveryStatus, parseTurnStreamEvent } from "../src/lib/contracts.ts";
import { readTurnStream, responseError } from "../src/lib/playerResponses.ts";

if (!process.argv[2]) throw new Error("A real backend corpus file is required.");
const { responses } = JSON.parse(await readFile(process.argv[2], "utf8"));
const parsers = { View: parseView, History: parseHistory, TurnFailure: parseTurnFailure,
  TurnRecoveryStatus: parseTurnRecoveryStatus, TurnStreamEvent: parseTurnStreamEvent };
assert.deepEqual(new Set(responses.map((item) => item.kind)), new Set(Object.keys(parsers)));
let rejected = 0;
function reject(parser, payload, mutate) {
  const value = structuredClone(payload);
  mutate(value);
  const before = structuredClone(value);
  assert.throws(() => parser(value), ContractError);
  assert.deepEqual(value, before);
  rejected++;
}
for (const { kind, payload } of responses) {
  assert.ok(parsers[kind], `Unknown corpus kind ${kind}`);
  const parser = kind === "TurnRecoveryStatus"
    ? (value) => parseTurnRecoveryStatus(value, payload.request_id)
    : parsers[kind];
  const before = structuredClone(payload);
  assert.equal(parser(payload), payload);
  assert.deepEqual(payload, before);
  reject(parser, payload, (value) => { value.unexpected = true; });
  if (kind === "View") {
    reject(parser, payload, (value) => { delete value.game_id; });
    reject(parser, payload, (value) => { delete value.interaction; });
    reject(parser, payload, (value) => { value.state_version = "1"; });
    reject(parser, payload, (value) => { value.complete = 0; });
    reject(parser, payload, (value) => { value.day = false; });
    reject(parser, payload, (value) => { value.billing = null; });
    if (payload.billing) reject(parser, payload, (value) => { delete value.billing.model_calls; });
    if (payload.status_fields.length) {
      reject(parser, payload, (value) => { value.status_fields[0].value = null; });
      reject(parser, payload, (value) => { value.status_fields[0].value = 0.5; });
    }
    if (payload.action_options.length) reject(parser, payload, (value) => { delete value.action_options[0].input; });
    if (payload.interaction?.options.length) reject(parser, payload, (value) => { value.interaction.options[0].enabled = 1; });
  } else if (kind === "History") {
    reject(parser, payload, (value) => { delete value.intro.segments; });
    if (payload.turns.length) reject(parser, payload, (value) => { value.turns[0].response.tick = 0.5; });
  } else if (kind === "TurnFailure") {
    reject(parser, payload, (value) => { delete value.code; });
    reject(parser, payload, (value) => { value.retryable = "false"; });
    reject(parser, payload, (value) => { value.commit_state = "maybe"; });
    assert.deepEqual(responseError(400, payload).failure, payload);
  } else if (kind === "TurnRecoveryStatus") {
    reject(parser, payload, (value) => { delete value.request_id; });
    reject(parser, payload, (value) => { delete value.status; });
    reject(parser, payload, (value) => { delete value.reason_code; });
    reject(parser, payload, (value) => { delete value.can_recover; });
    reject(parser, payload, (value) => { value.status = "future"; });
    reject(parser, payload, (value) => { value.reason_code = "FUTURE"; });
    reject(parser, payload, (value) => { value.can_recover = !payload.can_recover; });
    reject(parser, payload, (value) => { value.status = payload.status === "complete" ? "blocked" : "complete"; });
    reject(parser, payload, (value) => { value.request_id = `${payload.request_id}-mismatch`; });
  } else {
    reject(parser, payload, (value) => { value.type = "future"; });
    if (payload.type === "complete") reject(parser, payload, (value) => { delete value.view.body; });
    if (payload.type === "error") reject(parser, payload, (value) => { delete value.request_id; });
    if (payload.type === "preview") reject(parser, payload, (value) => { delete value.body; });
    if (payload.type === "segment") reject(parser, payload, (value) => { delete value.segment.kind; });
    if (payload.type === "stage") reject(parser, payload, (value) => { delete value.stage; });
  }
}
const names = new Set(responses.map((item) => item.name));
for (const name of ["create:freeform:default", "create:campaign:default", "create:campaign:freeform",
  "create:source:campaign", "create:source:freeform", "resume:intro", "resume:billed", "history:billed",
  "turn:freeform", "replay:freeform", "turn:source:campaign", "turn:source:freeform", "preview:create", "preview:turn",
  "failure:input:False", "failure:recovery:False", "failure:model:False", "failure:stopping:False", "failure:stopping:True",
  "status:complete", "status:mismatch", "status:no-snapshot", "status:legacy"]) {
  assert.ok(names.has(name), `Missing actual handler response: ${name}`);
}
const recoveryStatuses = responses.filter((item) => item.kind === "TurnRecoveryStatus");
assert.deepEqual(new Set(recoveryStatuses.map((item) => item.payload.reason_code)),
  new Set(["RECEIPT_READY", "INPUT_MISMATCH", "NO_SNAPSHOT", "LEGACY_REQUEST"]));
const events = responses.filter((item) => item.kind === "TurnStreamEvent").map((item) => item.payload);
assert.deepEqual(new Set(events.map((event) => event.type)), new Set(["stage", "segment", "preview", "complete", "error"]));
const streamed = responses.filter((item) => item.name.startsWith("stream:freeform:")).map((item) => item.payload);
const frame = streamed.map((event) => `data: ${JSON.stringify(event)}\n\n`).join("");
const emitted = [];
const finalView = await readTurnStream(new Response(frame), (event) => emitted.push(event));
assert.deepEqual(emitted, streamed);
assert.deepEqual(finalView, streamed.at(-1).view);
console.log(`Real backend corpus: ${responses.length} responses/events accepted; ${rejected} mutated payloads rejected by production parsers.`);
