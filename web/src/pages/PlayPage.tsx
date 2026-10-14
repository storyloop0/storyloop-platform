import { useEffect, useLayoutEffect, useRef, useState, type FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { ArrowLeft, ArrowUpRight, Feather, Moon, Plus, Send, SlidersHorizontal, Sparkles } from "lucide-react";
import { api, ApiError, type CastMember, type History, type SaveSettings, type StorySegment, type View } from "../lib/api";
import { errorMessage } from "../lib/session";
import { Button } from "../components/ui/button";
import { Loading, Notice } from "../components/Feedback";
import { StoryContent } from "../components/StoryContent";
import { CastPanel } from "../components/CastPanel";
import { PlayerCardPanel, usePlayerCard } from "../components/PlayerCardPanel";
import { ChoicePanel } from "../components/ChoicePanel";
import { containsStoryCommand, isStoryCommandInput, playerFacingText, storyLineBreaks } from "../lib/playerText";
import { clearPendingTurn, discardUnstartedTurn, readPendingTurn, savePendingTurn } from "../lib/pendingTurn";
import { createRequestId } from "../lib/requestId";
import { limitTurnInput } from "../lib/turnInput";
import { allowRecovery, inputHash, matchesRecovery, upsertCompletedTurn, type RecoveryProof } from "../lib/turnRecovery";

function playerAction(text: string | null, previous: View | null | undefined) {
  if (!text) return "早期存档的行动记录不可用";
  if (text === "/next") return "推进到下一时段";
  if (text === "/rest") return "休息到第二天";
  if (text === "/continue") return previous?.interaction?.label || "继续阅读";
  if (text.startsWith("/choose ")) {
    const [, id, ...rest] = text.split(/\s+/);
    const label = previous?.interaction?.options.find(
      (item) => item.id === id,
    )?.label;
    return label
      ? `选择「${playerFacingText(label)}」${rest.length ? ` · ${playerFacingText(rest.join(" "))}` : ""}`
      : "完成剧情选择";
  }
  return playerFacingText(text);
}

type PendingTurn = {
  id: string;
  text: string;
  stage: string;
  segments: StorySegment[];
  body: string;
  error: string;
  retryable?: boolean;
  startedAt: number;
};

const stageLabels: Record<string, string> = {
  received: "行动已送达",
  campaign: "检查当前剧情",
  thinking: "理解你的行动",
  adjudicating: "判断行动结果",
  committing: "记录世界变化",
  characters: "角色正在回应",
  background: "处理背景事件",
  narrating: "整理本轮故事",
  status: "结算当前数值",
  scene: "铺陈当前场景",
  guidance: "准备后续建议",
};

export function PlayPage() {
  const { gameId = "" } = useParams();
  const [history, setHistory] = useState<History | null>(null);
  const [current, setCurrent] = useState<View | null>(null);
  const [cast, setCast] = useState<CastMember[]>([]);
  const [title, setTitle] = useState("正在载入故事");
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [pending, setPending] = useState<PendingTurn | null>(null);
  const [recoveryProof, setRecoveryProof] = useState<RecoveryProof | null>(null);
  const recoveryGeneration = useRef(0);
  const sendingRef = useRef(false);
  const [now, setNow] = useState(Date.now());
  const [settings, setSettings] = useState<SaveSettings | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const [settingsError, setSettingsError] = useState("");
  const [settingsSaved, setSettingsSaved] = useState(false);
  const [temperature, setTemperature] = useState(1);
  const [contextWindow, setContextWindow] = useState(65536);
  const bottomRef = useRef<HTMLDivElement>(null);
  const playerCard = usePlayerCard(gameId, current?.state_version ?? 0);

  useEffect(() => {
    let active = true;
    setSettingsLoading(true);
    api.saveSettings(gameId).then((value) => {
      if (!active) return;
      setSettings(value);
      setTemperature(value.temperature);
      setContextWindow(value.context_window_tokens);
      setSettingsError("");
    }).catch((cause) => {
      if (active) setSettingsError(errorMessage(cause));
    }).finally(() => {
      if (active) setSettingsLoading(false);
    });
    return () => { active = false; };
  }, [gameId]);

  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  useEffect(() => {
    if (!current) return;
    let active = true;
    api.cast(gameId).then(({ cast: members }) => {
      if (active) setCast(members);
    }).catch(() => {
      if (active) setCast([]);
    });
    return () => { active = false; };
  }, [gameId, current?.state_version]);

  useEffect(() => {
    let active = true;
    const savedPending = readPendingTurn(gameId);
    setLoading(true);
    setError("");
    Promise.all([api.saves(), api.resume(gameId)])
      .then(async ([saveList, view]) => {
        const transcript = await api.history(gameId);
        if (!active) return;
        setTitle(
          saveList.saves.find((save) => save.game_id === gameId)?.title ||
            "我的故事",
        );
        setHistory(transcript);
        setCurrent(view);
        if (savedPending && transcript.turns.some((turn) => turn.request_id === savedPending.id)) {
          clearPendingTurn(gameId);
          setPending(null);
        } else {
          setPending(savedPending ? {
            ...savedPending, stage: "received", segments: [], body: "",
            error: savedPending.error || "上次行动的结果尚未确认。请用原请求重试。", startedAt: Date.now(),
          } : null);
        }
      })
      .catch((cause) => {
        if (active) setError(errorMessage(cause));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [gameId, retry]);

  useEffect(() => {
    const generation = ++recoveryGeneration.current;
    setRecoveryProof(null);
    if (!pending?.error || busy) return;
    const controller = new AbortController();
    const requestId = pending.id;
    const text = pending.text;
    void inputHash(text)
      .then((hash) => {
        if (controller.signal.aborted) return null;
        return api.getTurnRecoveryStatus(gameId, requestId, hash, controller.signal);
      })
      .then((status) => {
        if (!status || controller.signal.aborted || recoveryGeneration.current !== generation || sendingRef.current) return;
        setRecoveryProof({ gameId, requestId, text, generation, status });
      })
      .catch(() => {
        // The original pending request stays locked when proof cannot be read or validated.
      });
    return () => controller.abort();
  }, [gameId, pending?.id, pending?.text, pending?.error, busy]);

  useLayoutEffect(() => {
    if (!history?.turns.length && !pending) return;
    // Keep the true end of the reader in view. Smooth scrolling on every SSE
    // stage raced with newly inserted prose, options and the composer.
    const scrollToEnd = () => bottomRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
    scrollToEnd();
    const frame = window.requestAnimationFrame(scrollToEnd);
    return () => window.cancelAnimationFrame(frame);
  }, [history?.turns.length, pending?.stage, pending?.segments.length,
      pending?.body, current?.interaction?.id, current?.action_options]);

  async function submitText(text: string, recover = false): Promise<boolean> {
    if (!text.trim() || busy || sendingRef.current || (current?.complete && !pending?.error)) return false;
    const recoveryAllowed = recover && matchesRecovery(
      recoveryProof, gameId, pending, recoveryGeneration.current, busy,
    );
    if (pending && (!pending.error || pending.text !== text ||
      (pending.retryable === false && !recoveryAllowed))) return false;
    if (recover && !recoveryAllowed) return false;
    const keepNonretryableLock = pending?.retryable === false;
    setError("");
    setRecoveryProof(null);
    let requestId: string;
    try {
      requestId = pending?.id ?? createRequestId();
    } catch {
      setError("浏览器无法生成请求标识，请刷新页面后重试。");
      return false;
    }
    sendingRef.current = true;
    setBusy(true);
    savePendingTurn(gameId, keepNonretryableLock
      ? { id: requestId, text, retryable: false, error: pending?.error }
      : { id: requestId, text });
    setPending({
      id: requestId, text, stage: "received", segments: [], body: "", error: "",
      retryable: keepNonretryableLock ? false : undefined, startedAt: Date.now(),
    });
    if (!isStoryCommandInput(text)) setDraft("");
    try {
      const response = await api.turnStream(gameId, text, requestId, (event) => {
        setPending((previous) => {
          if (!previous || previous.id !== requestId) return previous;
          if (event.type === "stage") return { ...previous, stage: event.stage };
          if (event.type === "segment")
            return { ...previous, segments: [...previous.segments, event.segment] };
          if (event.type === "preview")
            return { ...previous, segments: event.segments, body: event.body };
          return previous;
        });
      });
      setHistory((previous) =>
        previous
          ? {
              ...previous,
              turns: upsertCompletedTurn(previous.turns, requestId, text, response),
            }
          : previous,
      );
      setCurrent(response);
      setDraft("");
      window.dispatchEvent(new Event("story:billing-updated"));
      clearPendingTurn(gameId);
      setPending(null);
      return true;
    } catch (cause) {
      if (!keepNonretryableLock && cause instanceof ApiError &&
          discardUnstartedTurn(gameId, requestId, cause.failure)) {
        setPending(null);
        setDraft(text);
        setError(errorMessage(cause));
        return false;
      }
      if (keepNonretryableLock || (cause instanceof ApiError && cause.failure?.retryable === false &&
          cause.failure.request_id === requestId)) {
        savePendingTurn(gameId, { id: requestId, text, retryable: false, error: errorMessage(cause) });
      }
      setPending((previous) => previous?.id === requestId
        ? { ...previous, error: errorMessage(cause),
            retryable: keepNonretryableLock ? false
              : cause instanceof ApiError ? cause.failure?.retryable : undefined } : previous);
      return false;
    } finally {
      sendingRef.current = false;
      setBusy(false);
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    await submitText(draft.trim());
  }

  function useSuggestion(text: string) {
    setDraft(text);
    document.getElementById("turn-input")?.focus();
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault();
    if (!settings || settingsSaving || busy || pending) return;
    setSettingsSaving(true);
    setSettingsError("");
    setSettingsSaved(false);
    try {
      const updated = await api.updateSaveSettings(gameId, temperature, contextWindow);
      setSettings(updated);
      setTemperature(updated.temperature);
      setContextWindow(updated.context_window_tokens);
      setSettingsSaved(true);
    } catch (cause) {
      setSettingsError(errorMessage(cause));
    } finally {
      setSettingsSaving(false);
    }
  }

  const intro = history?.intro;
  const visibleSuggestions = current?.suggestions.filter(
    (suggestion) => !containsStoryCommand(suggestion),
  ) ?? [];
  const actionOptions = current?.action_options ?? [];

  return (
    <main className="reader-shell">
      <div className="reader-top">
        <Link to="/saves" className="back-link">
          <ArrowLeft size={17} />
          返回存档
        </Link>
        <div className="reader-top-actions">
          <span className="reader-top-meta">
            {current?.day
              ? `第 ${current.day} 天${current.time_of_day ? ` · ${current.time_of_day}` : ""}`
              : `第 ${current?.tick ?? 0} 回合`}
          </span>
          <button type="button" className="reader-settings-toggle"
            aria-expanded={settingsOpen} aria-controls="reader-settings"
            onClick={() => setSettingsOpen((open) => !open)}>
            <SlidersHorizontal size={16} aria-hidden="true" /> 游玩设置
          </button>
        </div>
      </div>
      {settingsOpen && (
        <section className="reader-settings" id="reader-settings" aria-label="游玩设置">
          <div className="reader-settings-heading">
            <h2>生成设置</h2>
            <p>只影响这个存档，从下一次行动开始生效。</p>
          </div>
          {settingsLoading ? <p>正在读取设置…</p> : settings ? (
            <form onSubmit={saveSettings}>
              <div className="reader-settings-fields">
                <label>
                  <span>模型热度 <strong>{temperature.toFixed(1)}</strong></span>
                  <input type="range" min="0" max="1.9" step="0.1" value={temperature}
                    onChange={(event) => { setTemperature(Number(event.target.value)); setSettingsSaved(false); }}
                    disabled={settingsSaving || busy || !!pending} />
                  <small>越低越稳定，越高越有变化；较高热度可能降低行动判断的一致性。</small>
                </label>
                <label>
                  <span>上下文窗口</span>
                  <select value={contextWindow}
                    onChange={(event) => { setContextWindow(Number(event.target.value)); setSettingsSaved(false); }}
                    disabled={settingsSaving || busy || !!pending}>
                    {[...new Set([8192, 16384, 32768, 65536, 131072, 262144, 524288,
                      1000000, settings.max_context_window_tokens, settings.context_window_tokens])]
                      .filter((value) => value <= settings.max_context_window_tokens)
                      .sort((a, b) => a - b)
                      .map((value) => <option value={value} key={value}>{value.toLocaleString()} Token</option>)}
                  </select>
                  <small>这是故事上下文预算，系统会预留回复空间。越大越晚压缩历史，也可能增加每轮输入 Token、等待时间和积分消耗。</small>
                </label>
              </div>
              <div className="reader-settings-footer">
                <Button type="submit" disabled={settingsSaving || busy || !!pending ||
                  (temperature === settings.temperature && contextWindow === settings.context_window_tokens)}>
                  {settingsSaving ? "正在保存…" : "保存设置"}
                </Button>
                {settingsSaved && <span role="status">已保存，下回合生效</span>}
                {settingsError && <span className="form-error" role="alert">{settingsError}</span>}
              </div>
            </form>
          ) : <p className="form-error" role="alert">{settingsError || "无法读取设置"}</p>}
        </section>
      )}
      {loading ? (
        <Loading label="正在恢复你的故事…" />
      ) : !history || !current ? (
        <Notice
          message={error || "无法读取这个存档。"}
          onRetry={() => setRetry((value) => value + 1)}
        />
      ) : (
        <div className="reader-layout">
          <section className="reading-column" aria-label="故事内容">
            <div className="story-heading">
              <span className="section-label">正在阅读</span>
              <h1>{title}</h1>
              <p>每一页，都因你的选择而不同。</p>
            </div>
            <div className="player-card-mobile">
              <PlayerCardPanel card={playerCard.card} loading={playerCard.loading}
                error={playerCard.error} onRetry={playerCard.retry} />
            </div>
            <div className="story-timeline">
              {intro && (intro.opening || intro.body) && (
                <article className="story-entry intro-entry">
                  <div className="entry-marker">
                    <Sparkles size={17} />
                  </div>
                  <div className="entry-content">
                    <span className="entry-label">序章</span>
                    {intro.opening && (
                      <div className="story-prose">
                        {storyLineBreaks(playerFacingText(intro.opening)).split(/\n\s*\n/).filter(Boolean).map((paragraph, index) => (
                          <p key={index}>{paragraph}</p>
                        ))}
                      </div>
                    )}
                    <StoryContent view={intro} cast={cast} />
                  </div>
                </article>
              )}
              {history.turns.map((turn, index) => (
                <div className="turn-pair" key={turn.request_id}>
                  <article className="player-entry">
                    <span className="player-entry-label">
                      你的行动 · {index + 1}
                    </span>
                    <p>
                      {playerAction(
                        turn.input,
                        index === 0
                          ? intro
                          : (history.turns[index - 1]?.response ?? null),
                      )}
                    </p>
                  </article>
                  <article className="story-entry">
                    <div className="entry-marker">
                      <Feather size={17} />
                    </div>
                    <div className="entry-content">
                      <span className="entry-label">故事回应</span>
                      <StoryContent view={turn.response} cast={cast} />
                      {turn.response.billing && (
                        <div className="turn-billing">
                          消耗 {turn.response.billing.charged_points} 积分
                          <span>·</span>
                          输入 {turn.response.billing.input_tokens.toLocaleString()} / 输出 {turn.response.billing.output_tokens.toLocaleString()} Token
                        </div>
                      )}
                    </div>
                  </article>
                </div>
              ))}
              {pending && (
                <div className="turn-pair pending-turn">
                  <article className="player-entry">
                    <span className="player-entry-label">你的行动 · {history.turns.length + 1}</span>
                    <p>{playerAction(pending.text, current)}</p>
                  </article>
                  <article className="story-entry" aria-live="polite">
                    <div className="entry-marker"><Feather size={17} /></div>
                    <div className="entry-content">
                      <span className="entry-label">故事回应</span>
                      <div className={`turn-status ${pending.error ? "turn-status-error" : ""}`} role="status">
                        <span className="status-pulse" aria-hidden="true" />
                        <span>{pending.error || stageLabels[pending.stage] || "故事正在继续"}</span>
                        {!pending.error && (
                          <time>{Math.max(0, Math.floor((now - pending.startedAt) / 1000))} 秒</time>
                        )}
                      </div>
                      {(pending.segments.length > 0 || pending.body) && (
                        <StoryContent view={{ ...current, body: pending.body, segments: pending.segments }} cast={cast} />
                      )}
                      {pending.error && (
                        <div className="pending-actions">
                          {matchesRecovery(recoveryProof, gameId, pending, recoveryGeneration.current, busy)
                            ? <Button type="button" disabled={busy || !allowRecovery(recoveryProof?.status ?? null, busy)}
                                onClick={() => void submitText(pending.text, true)}>
                                {recoveryProof?.status.status === "complete" ? "取回结果" : "继续恢复这条行动"}
                              </Button>
                            : pending.retryable !== false &&
                              <Button type="button" disabled={busy} onClick={() => void submitText(pending.text)}>重试这条行动</Button>}
                        </div>
                      )}
                    </div>
                  </article>
                </div>
              )}
              {!intro?.opening && !intro?.body && !history.turns.length && (
                <div className="empty-story">
                  <p>故事从你的第一句话开始。</p>
                </div>
              )}
            </div>
            {current.complete && (
              <div className="end-note">
                <Sparkles size={20} />
                <span>这一段故事已经写完。你可以返回目录，开始新的旅程。</span>
              </div>
            )}
            {!current.complete && !pending && current.interaction ? (
              <ChoicePanel
                key={current.interaction.id}
                interaction={current.interaction}
                busy={busy || !!pending}
                onChoose={submitText}
                initialInput={draft}
                error={error}
              />
            ) : (
              !current.complete && !pending && (
                <>
                {(actionOptions.length > 0 || visibleSuggestions.length > 0) && (
                  <section className="next-actions" aria-label="接下来的行动">
                    <div className="next-actions-heading">
                      <span className="section-label">接下来</span>
                      <p>沿着眼前的故事继续，也可以自己决定怎么做。</p>
                    </div>
                    <div className="next-actions-list">
                      {actionOptions.length > 0
                        ? actionOptions.map((option, index) => (
                            <button
                              type="button"
                              className="next-action"
                              key={`${index}-${option.label}`}
                              onClick={() => void submitText(option.input)}
                              disabled={busy || !!pending}
                            >
                              <span className="next-action-number">{String.fromCharCode(65 + index)}</span>
                              <span className="next-action-copy">
                                <strong>{option.label}</strong>
                                <small>{option.input}</small>
                              </span>
                              <ArrowUpRight size={16} aria-hidden="true" />
                            </button>
                          ))
                        : visibleSuggestions.map((suggestion, index) => (
                            <button
                              type="button"
                              className="next-action"
                              key={`${index}-${suggestion}`}
                              onClick={() => useSuggestion(suggestion)}
                              disabled={busy || !!pending}
                            >
                              <span className="next-action-number">{String.fromCharCode(65 + index)}</span>
                              <span className="next-action-copy"><strong>{suggestion}</strong></span>
                              <Plus size={16} aria-hidden="true" />
                            </button>
                          ))}
                    </div>
                  </section>
                )}
                {current.mode === "campaign" && current.time_of_day === "晚上" && (
                  <div className="night-action">
                    <span>夜色已深。你还可以继续行动，或准备休息。</span>
                    <button
                      type="button"
                      onClick={() => void submitText("/rest")}
                      disabled={busy || !!pending}
                    >
                      <Moon size={16} aria-hidden="true" />
                      准备休息
                    </button>
                  </div>
                )}
                <form className="composer" onSubmit={submit}>
                  <label htmlFor="turn-input">写下你的行动或想说的话</label>
                  <div className="composer-field">
                    <textarea
                      id="turn-input"
                      value={draft}
                      onChange={(event) => setDraft(limitTurnInput(event.target.value))}
                      placeholder="此刻，你想做什么？"
                      rows={3}
                      disabled={busy || !!pending || current.complete}
                      onKeyDown={(event) => {
                        if (
                          event.key === "Enter" &&
                          !event.shiftKey &&
                          !event.nativeEvent.isComposing
                        ) {
                          event.preventDefault();
                          event.currentTarget.form?.requestSubmit();
                        }
                      }}
                    />
                    <Button
                      type="submit"
                      size="icon"
                      aria-label="发送行动"
                      disabled={busy || !!pending || !draft.trim() || current.complete}
                    >
                      <Send size={18} />
                    </Button>
                  </div>
                  <div className="composer-hint">
                    <span>Enter 发送 · Shift + Enter 换行</span>
                    <span>
                      {busy ? "世界正在回应…" : `${Array.from(draft).length}/10000`}
                    </span>
                  </div>
                  {error && (
                    <p className="form-error" role="alert">
                      {error}
                    </p>
                  )}
                </form>
                </>
              )
            )}
            <div ref={bottomRef} aria-hidden="true" />
          </section>
          <aside className="reader-aside" aria-label="进度与建议">
            <div className="aside-panel">
              <span className="aside-kicker">当前进度</span>
              <div className="progress-value">
                {current.day
                  ? `第 ${current.day} 天${current.time_of_day ? ` · ${current.time_of_day}` : ""}`
                  : `第 ${current.tick} 回合`}
              </div>
              <p>世界仍在继续运转。</p>
              {!!current.status_fields?.length && (
                <div className="story-status" aria-label="当前数值">
                  <span className="story-status-heading">当前数值</span>
                  <dl>
                    {current.status_fields.map((field) => (
                      <div className="story-status-row" key={field.id}>
                        <dt>{field.label}</dt>
                        <dd>{typeof field.value === "boolean" ? (field.value ? "是" : "否") : field.value}</dd>
                      </div>
                    ))}
                  </dl>
                </div>
              )}
            </div>
            <div className="player-card-desktop">
              <PlayerCardPanel card={playerCard.card} loading={playerCard.loading}
                error={playerCard.error} onRetry={playerCard.retry} />
            </div>
            <CastPanel cast={cast} gameId={gameId} stateVersion={current.state_version} />
          </aside>
        </div>
      )}
    </main>
  );
}
