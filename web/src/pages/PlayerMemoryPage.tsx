import { useEffect, useState } from "react";
import { Pencil, RotateCcw, Sparkles, Trash2 } from "lucide-react";
import { api, type PlayerMemoryStatus } from "../lib/api";
import { Loading, Notice } from "../components/Feedback";

export function PlayerMemoryPage() {
  const [status, setStatus] = useState<PlayerMemoryStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    api.playerMemory().then((value) => {
      if (active) { setStatus(value); setError(""); }
    }).catch(() => {
      if (active) setError("画像状态暂时无法读取，请重试。");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [retry]);

  async function update(enabled: boolean) {
    setBusy(true);
    setError("");
    try { setStatus(await api.setPlayerMemory(enabled)); }
    catch { setError("设置未能保存，请稍后重试。"); }
    finally { setBusy(false); }
  }

  async function clear() {
    setBusy(true);
    setError("");
    try { setStatus(await api.clearPlayerMemory()); }
    catch { setError("清空未能完成，请稍后重试。"); }
    finally { setBusy(false); }
  }

  async function changeOne(action: () => Promise<PlayerMemoryStatus>, failure: string) {
    setBusy(true);
    setError("");
    try {
      setStatus(await action());
    } catch {
      try {
        setStatus(await api.playerMemory());
        setError(failure);
      } catch {
        setError("操作未能完成，画像状态也暂时无法刷新，请手动刷新。");
      }
    } finally {
      setEditingId(null);
      setConfirmDeleteId(null);
      setBusy(false);
    }
  }

  function dateLabel(value: string) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString("zh-CN");
  }

  return (
    <main className="page-container player-memory-page">
      <div className="page-title">
        <span className="section-label">账户</span>
        <h1>玩家画像</h1>
        <p>让故事逐渐了解你喜欢怎样游玩。由你决定是否启用。</p>
      </div>
      {loading ? <Loading label="正在读取画像设置…" /> : !status ? (
        <Notice message={error || "画像状态暂时无法读取。"} onRetry={() => setRetry((value) => value + 1)} />
      ) : (
        <>
          <section className="memory-settings" aria-labelledby="memory-heading">
            <div className="memory-heading-row">
              <span className="memory-icon"><Sparkles size={23} /></span>
              <div>
                <h2 id="memory-heading">跨故事的游玩偏好</h2>
                <p>只供主控与叙述参考，不会成为 NPC 的记忆或故事事实。</p>
              </div>
            </div>
            <div className="memory-explainer">
              <p>开启后，系统从你的有效行动中提炼节奏、文风与互动习惯。每累计 {status.batch_size} 条有效输入，且距离上次提炼至少 {status.min_interval_hours} 小时，才会启动一次后台提炼。</p>
              <p>{status.generated_preference_max_age_days === null ? "未启用自动过期" :
                `自动提炼的偏好会在创建 ${status.generated_preference_max_age_days} 天后停止供故事参考，记录仍可纠正或删除。`}</p>
              <p><strong>费用说明：</strong>提炼会产生百炼 API 费用；内测期间由平台承担，不扣你的积分。关闭后不再采集新输入，已生成偏好会保留，直到你纠正、删除或清空。</p>
            </div>
            <div className="memory-control-row">
              <div>
                <strong>{!status.available ? "平台暂未开放" : status.enabled ? "已开启" : "未开启"}</strong>
                <span>{!status.available ? "当前不会采集或生成画像。" : status.enabled
                  ? `已有 ${status.queued_inputs} 条输入等待下一次提炼。` : "当前不会采集新的游玩输入。"}</span>
              </div>
              {status.available && (
                <button type="button" className="memory-toggle" disabled={busy}
                  onClick={() => update(!status.enabled)}>
                  {status.enabled ? "关闭画像" : "开启画像"}
                </button>
              )}
            </div>
          </section>
          {(status.available || status.memories.length > 0 || status.queued_inputs > 0) && (
            <section className="memory-records" aria-labelledby="memory-records-heading">
              <div className="memory-records-head">
                <div>
                  <span className="section-label">当前记录</span>
                  <h2 id="memory-records-heading">关于你的偏好</h2>
                </div>
                <button type="button" className="memory-refresh" disabled={busy}
                  onClick={() => setRetry((value) => value + 1)}>
                  <RotateCcw size={15} /> 刷新
                </button>
              </div>
              {status.memories.length ? (
                <ul className="memory-list">
                  {status.memories.map((item) => <li key={item.id}>
                    {editingId === item.id ? (
                      <form className="memory-edit" onSubmit={(event) => {
                        event.preventDefault();
                        if (draft.trim().length > 0 && draft.trim().length <= 180) {
                          void changeOne(() => api.correctPlayerMemory(item.id, draft.trim()), "纠正未能完成，已重新读取当前画像，请重试。");
                        }
                      }}>
                        <label htmlFor={`memory-edit-${item.id}`}>偏好内容</label>
                        <textarea id={`memory-edit-${item.id}`} value={draft} maxLength={180}
                          disabled={busy} onChange={(event) => setDraft(event.target.value)} />
                        <div className="memory-edit-footer">
                          <span>{draft.trim().length}/180 字</span>
                          <div className="memory-row-actions">
                            <button type="button" disabled={busy} onClick={() => setEditingId(null)}>取消</button>
                            <button type="submit" disabled={busy || !draft.trim()}>保存纠正</button>
                          </div>
                        </div>
                      </form>
                    ) : (
                      <>
                        <p className="memory-row-text">{item.text}</p>
                        <p className="memory-row-freshness">{item.freshness === "expired" ? "已过期，不再供故事参考" :
                          item.freshness === "unknown" ? "时间信息不足，暂不供故事参考" :
                          item.source === "user_correction" ? "你已纠正，持续有效" : "有效"}</p>
                        <div className="memory-row-footer">
                          <span className="memory-row-meta">
                            {item.source === "user_correction" ? "你已纠正" : "故事提炼"}
                            {dateLabel(item.updated_at || item.created_at) && ` · ${dateLabel(item.updated_at || item.created_at)}`}
                          </span>
                          {confirmDeleteId === item.id ? (
                            <div className="memory-delete-confirm">
                              <span>确定删除这条偏好？</span>
                              <button type="button" disabled={busy} onClick={() => setConfirmDeleteId(null)}>取消</button>
                              <button type="button" disabled={busy}
                                onClick={() => void changeOne(() => api.deletePlayerMemory(item.id), "删除未能完成，已重新读取当前画像，请重试。")}>确认删除</button>
                            </div>
                          ) : (
                            <div className="memory-row-actions">
                              <button type="button" disabled={busy} onClick={() => {
                                setEditingId(item.id); setDraft(item.text); setConfirmDeleteId(null);
                              }}><Pencil size={14} /> 纠正偏好</button>
                              <button type="button" disabled={busy} onClick={() => setConfirmDeleteId(item.id)}>
                                <Trash2 size={14} /> 删除偏好
                              </button>
                            </div>
                          )}
                        </div>
                      </>
                    )}
                  </li>)}
                </ul>
              ) : <p className="memory-empty">还没有提炼出偏好。游玩一段时间后可以回来看看。</p>}
              {status.memories.length > 0 && <p className="memory-edit-note">纠正或删除一条偏好时，这次尚未提炼的游玩输入也会清除。</p>}
              {(status.memories.length > 0 || status.queued_inputs > 0) && (
                <button type="button" className="memory-clear" disabled={busy} onClick={clear}>
                  <Trash2 size={15} /> 清空画像和待提炼输入
                </button>
              )}
            </section>
          )}
          {error && <p className="form-error" role="alert">{error}</p>}
        </>
      )}
    </main>
  );
}
