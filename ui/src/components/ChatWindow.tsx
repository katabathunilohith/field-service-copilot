import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, formatDate } from "../api";
import type { AgentRun, Catalog, CitationReport, MemoryDelta, Mode, RetainResult, StreamEvent, ToolTrace, ViewMode } from "../types";
import { Badge, Icon, Markdown, OutcomeBadge, Spinner, statusTone } from "./ui";
import VoiceButton from "./VoiceButton";

type Outcome = { state: "saving" | "done" | "error"; held?: boolean; retain?: RetainResult; error?: string };
type Msg =
  | { id: string; kind: "user"; text: string; technician: string }
  | { id: string; kind: "run"; run: AgentRun; outcome?: Outcome }
  | { id: string; kind: "error"; text: string };

type StepState = "running" | "done" | "warn" | "info";
type Step = { key: string; label: string; detail?: string; state: StepState };
type Live = { steps: Step[]; answer?: string; citations?: CitationReport | null };

type Threads = Record<Mode, Msg[]>;
type Turn = { role: "user" | "assistant"; content: string };

const MODE_LABEL: Record<ViewMode, string> = {
  compare: "Side by side",
  baseline: "Baseline only",
  copilot: "Copilot only",
};

let seq = 0;
const nextId = () => `m${++seq}`;

interface Props {
  catalog: Catalog | null;
  voiceEnabled: boolean;
  onRun: (run: AgentRun) => void;
  onMemoryChanged: () => void;
  onInspect: () => void;
}

function upsert(steps: Step[], step: Step): Step[] {
  const i = steps.findIndex((s) => s.key === step.key);
  if (i === -1) return [...steps, step];
  const next = steps.slice();
  next[i] = { ...next[i], ...step };
  return next;
}

/** Fold one server-sent event into the live pipeline view of a pane. */
function applyEvent(live: Live, e: StreamEvent): Live {
  const steps = live.steps;
  switch (e.event) {
    case "parsed": {
      const p = e.data.parsed;
      const what = [p.unit_id, p.error_code, p.model_name].filter(Boolean).join(" · ");
      return { ...live, steps: upsert(steps, { key: "parse", label: "Understood the question", detail: what || "no unit or code found", state: what ? "done" : "warn" }) };
    }
    case "tool": {
      const d = e.data;
      if (d.prefetched) return { ...live, steps: upsert(steps, { key: "telemetry", label: "Pulled live telemetry", detail: "prefetched", state: "done" }) };
      if (d.stage === "work order") return live;
      const key = `tool-${d.name}`;
      return d.phase === "start"
        ? { ...live, steps: upsert(steps, { key, label: `Calling ${d.name}`, detail: d.repaired ? "arguments auto-repaired" : undefined, state: "running" }) }
        : { ...live, steps: upsert(steps, { key, label: `Called ${d.name}`, detail: `${d.latency_ms ?? 0} ms${d.repaired ? " · repaired" : ""}`, state: d.error ? "warn" : "done" }) };
    }
    case "recall": {
      if (e.data.status === "start") return { ...live, steps: upsert(steps, { key: "recall", label: "Recalling fleet memory", state: "running" }) };
      const cached = e.data.recalls?.some((r) => r.status === "cached");
      const local = e.data.source !== "hindsight";
      const why = e.data.recalls?.find((r) => r.source !== "hindsight")?.status;
      return {
        ...live,
        steps: upsert(steps, {
          key: "recall",
          label: `Recalled ${e.data.count} memories`,
          detail: local
            ? `local fallback${why ? ` (${why})` : ""} · ${e.data.latency_ms} ms`
            : `Hindsight · ${e.data.latency_ms} ms${cached ? " · prefetched" : ""}`,
          state: local ? "warn" : "done",
        }),
      };
    }
    case "reflect": {
      const fromHindsight = e.data.source === "hindsight";
      return {
        ...live,
        steps: upsert(steps, {
          key: "reflect",
          label: fromHindsight ? "Hindsight reflection applied" : "Pattern synthesis (local)",
          detail: fromHindsight ? (e.data.cached ? "cached" : "fresh") : "Hindsight is reflecting in the background",
          state: fromHindsight ? "done" : "info",
        }),
      };
    }
    case "llm":
      return { ...live, steps: upsert(steps, { key: "llm", label: `Reasoning with ${e.data.model}`, detail: e.data.round > 1 ? `round ${e.data.round}` : undefined, state: "running" }) };
    case "llm_wait":
      return { ...live, steps: upsert(steps, { key: "wait", label: `Pacing for the Groq rate limit (${e.data.seconds}s)`, detail: e.data.reason, state: "info" }) };
    case "answer":
      return { ...live, answer: e.data.text, citations: e.data.citations, steps: upsert(steps, { key: "llm", label: "Answer ready", state: "done" }) };
    case "ticket":
      return { ...live, steps: upsert(steps, { key: "ticket", label: `Filed work order ${e.data.ticket.id}`, detail: e.data.ticket.created_via === "agent_tool" ? "via log_repair_ticket" : "auto", state: "done" }) };
    case "retain":
      return {
        ...live,
        steps: upsert(steps, {
          key: "retain",
          label: e.data.source === "hindsight" ? "Session retained to Hindsight" : "Session saved to the outbox",
          detail: e.data.source === "hindsight" ? e.data.status : "syncs to Hindsight automatically",
          state: e.data.source === "hindsight" ? "done" : "info",
        }),
      };
    default:
      return live;
  }
}

export default function ChatWindow({ catalog, voiceEnabled, onRun, onMemoryChanged, onInspect }: Props) {
  const [view, setView] = useState<ViewMode>("compare");
  const [technician, setTechnician] = useState("Tech_Alex");
  const [unit, setUnit] = useState<string>("");
  const [text, setText] = useState("");
  const [threads, setThreads] = useState<Threads>({ baseline: [], copilot: [] });
  const [live, setLive] = useState<Record<Mode, Live | null>>({ baseline: null, copilot: null });
  const [notice, setNotice] = useState<string | null>(null);
  const lastPrefetch = useRef("");

  const busy = live.baseline !== null || live.copilot !== null;
  const panes: Mode[] = view === "compare" ? ["baseline", "copilot"] : [view];
  const empty = threads.baseline.length === 0 && threads.copilot.length === 0;

  const unitsByModel = useMemo(() => {
    const groups = new Map<string, Catalog["units"]>();
    for (const u of catalog?.units ?? []) groups.set(u.model_name, [...(groups.get(u.model_name) ?? []), u]);
    return [...groups.entries()];
  }, [catalog]);

  // Warm Hindsight recall while the technician is still typing (the answer then starts from cache).
  useEffect(() => {
    if (view === "baseline" || text.trim().length < 12) return;
    const key = `${technician}|${unit}|${text.trim()}`;
    const timer = window.setTimeout(() => {
      if (key === lastPrefetch.current) return;
      lastPrefetch.current = key;
      void api.prefetch(text.trim(), technician, unit || null);
    }, 700);
    return () => window.clearTimeout(timer);
  }, [text, unit, technician, view]);

  const append = (mode: Mode, msg: Msg) => setThreads((t) => ({ ...t, [mode]: [...t[mode], msg] }));

  const historyFor = (mode: Mode): Turn[] =>
    threads[mode]
      .flatMap((m): Turn[] =>
        m.kind === "user" ? [{ role: "user", content: m.text }] : m.kind === "run" ? [{ role: "assistant", content: m.run.answer }] : [],
      )
      .slice(-6);

  async function submit(query: string) {
    const q = query.trim();
    if (q.length < 3 || busy) return;
    setText("");
    setNotice(null);
    const modes = panes;
    for (const mode of modes) append(mode, { id: nextId(), kind: "user", text: q, technician });
    setLive((l) => ({ ...l, ...Object.fromEntries(modes.map((m) => [m, { steps: [] }])) }));

    // One stream per pane so each renders as its pipeline progresses.
    await Promise.all(
      modes.map(async (mode) => {
        try {
          const run = await api.diagnoseStream(
            { query: q, technician_id: technician, unit_id: unit || null, mode, history: { [mode]: historyFor(mode) } },
            (e) => setLive((l) => (l[mode] ? { ...l, [mode]: applyEvent(l[mode]!, e) } : l)),
          );
          append(mode, { id: nextId(), kind: "run", run });
          if (mode === "copilot") onRun(run);
        } catch (err) {
          append(mode, { id: nextId(), kind: "error", text: err instanceof ApiError ? err.message : String(err) });
        } finally {
          setLive((l) => ({ ...l, [mode]: null }));
        }
      }),
    );
    onMemoryChanged();
  }

  async function confirm(msgId: string, run: AgentRun, held: boolean) {
    if (!run.ticket) return;
    const update = (outcome: Outcome) =>
      setThreads((t) => ({
        ...t,
        copilot: t.copilot.map((m) => (m.id === msgId && m.kind === "run" ? { ...m, outcome } : m)),
      }));
    update({ state: "saving", held });
    try {
      const resp = await api.confirmOutcome(run.ticket.id, held, run.technician.id);
      update({ state: "done", held, retain: resp.retain });
      onMemoryChanged();
    } catch (err) {
      update({ state: "error", held, error: err instanceof Error ? err.message : String(err) });
    }
  }

  function applySample(sample: Catalog["sample_prompts"][number]) {
    setTechnician(sample.technician);
    setUnit(sample.unit);
    setText(sample.text);
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col gap-3" aria-label="Diagnostic chat">
      {/* Controls */}
      <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
        <label className="flex min-w-40 flex-col gap-1 text-xs font-medium text-ink-2">
          Technician
          <select
            value={technician}
            onChange={(e) => setTechnician(e.target.value)}
            className="h-9 rounded-md border border-line bg-surface px-2 text-sm text-ink"
          >
            {(catalog?.technicians ?? []).map((t) => (
              <option key={t.id} value={t.id}>
                {t.id} · {t.role}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-52 flex-col gap-1 text-xs font-medium text-ink-2">
          Unit
          <select
            value={unit}
            onChange={(e) => setUnit(e.target.value)}
            className="h-9 rounded-md border border-line bg-surface px-2 text-sm text-ink"
          >
            <option value="">Detect from question</option>
            {unitsByModel.map(([model, units]) => (
              <optgroup key={model} label={model}>
                {units.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.id} · {u.site}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>
        <div className="flex flex-col gap-1 text-xs font-medium text-ink-2">
          <span id="view-label">Compare</span>
          <div role="radiogroup" aria-labelledby="view-label" className="flex h-9 rounded-md border border-line bg-surface-2 p-0.5">
            {(["compare", "baseline", "copilot"] as ViewMode[]).map((v) => (
              <button
                key={v}
                role="radio"
                aria-checked={view === v}
                onClick={() => setView(v)}
                disabled={busy}
                className={`rounded px-3 text-sm transition-colors disabled:opacity-60 ${
                  view === v ? "bg-surface font-medium text-ink shadow-sm" : "text-ink-2 hover:text-ink"
                }`}
              >
                {MODE_LABEL[v]}
              </button>
            ))}
          </div>
        </div>
        {!empty && (
          <button
            onClick={() => setThreads({ baseline: [], copilot: [] })}
            disabled={busy}
            className="ml-auto h-9 rounded-md border border-line px-3 text-sm text-ink-2 hover:bg-surface-2 disabled:opacity-50"
          >
            New session
          </button>
        )}
      </div>

      {/* Panes */}
      <div className={`grid min-h-0 flex-1 gap-3 ${panes.length === 2 ? "lg:grid-cols-2" : ""}`}>
        {panes.map((mode) => (
          <Pane key={mode} mode={mode} messages={threads[mode]} live={live[mode]} onConfirm={confirm} onInspect={onInspect} />
        ))}
      </div>

      {/* Composer */}
      <div className="sticky bottom-3 z-10 rounded-xl border border-line bg-surface p-2 shadow-panel lg:static">
        {catalog && (
          <div className="flex gap-2 overflow-x-auto px-1 pb-2" aria-label="Example questions">
            {catalog.sample_prompts.map((s) => (
              <button
                key={s.text}
                onClick={() => applySample(s)}
                className="shrink-0 rounded-full border border-line bg-surface-2 px-3 py-1 text-xs text-ink-2 hover:border-line-strong hover:text-ink"
                title={s.text}
              >
                {s.technician.replace("Tech_", "")} · {s.unit}
              </button>
            ))}
          </div>
        )}
        {notice && <p className="px-2 pb-1.5 text-xs text-ink-2">{notice}</p>}
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void submit(text);
          }}
        >
          <label htmlFor="composer" className="sr-only">
            Describe the fault
          </label>
          <textarea
            id="composer"
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void submit(text);
              }
            }}
            rows={2}
            maxLength={2000}
            placeholder="Describe the unit, the error code and what you see, or tap the mic. e.g. “30XA CHL-0417 tripping E-412 again”"
            className="min-h-11 flex-1 resize-none rounded-lg bg-transparent px-2 py-1.5 text-sm text-ink placeholder:text-ink-3 focus:outline-none"
          />
          {voiceEnabled && (
            <VoiceButton
              disabled={busy}
              onTranscript={(t) => {
                setText((prev) => (prev.trim() ? `${prev.trim()} ${t.text}` : t.text));
                if (t.parsed.unit_id) setUnit(t.parsed.unit_id);
                setNotice(
                  `Heard in ${(t.latency_ms / 1000).toFixed(1)}s${t.parsed.unit_id || t.parsed.error_code ? ` · detected ${[t.parsed.unit_id, t.parsed.error_code].filter(Boolean).join(" · ")}` : ""}. Review, then press Diagnose.`,
                );
              }}
              onError={setNotice}
            />
          )}
          <button
            type="submit"
            disabled={busy || text.trim().length < 3}
            className="flex h-10 items-center gap-1.5 rounded-lg bg-accent px-4 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
          >
            <Icon name="send" className="size-4" />
            Diagnose
          </button>
        </form>
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ pane
function Pane({
  mode,
  messages,
  live,
  onConfirm,
  onInspect,
}: {
  mode: Mode;
  messages: Msg[];
  live: Live | null;
  onConfirm: (msgId: string, run: AgentRun, held: boolean) => void;
  onInspect: () => void;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "smooth" });
  }, [messages.length, live?.steps.length, live?.answer]);

  const copilot = mode === "copilot";
  return (
    <div
      className={`flex min-h-[26rem] flex-col overflow-hidden rounded-xl border bg-surface shadow-panel lg:min-h-0 ${
        copilot ? "border-accent-line" : "border-line"
      }`}
    >
      <header className={`flex items-center gap-2 border-b px-4 py-2.5 ${copilot ? "border-accent-line bg-accent-wash" : "border-line"}`}>
        <Icon name={copilot ? "memory" : "book"} className={`size-4 ${copilot ? "text-accent-ink" : "text-ink-2"}`} />
        <h2 className="text-sm font-semibold">{copilot ? "Hindsight Copilot" : "Baseline agent"}</h2>
        <span className="truncate text-xs text-ink-2">
          {copilot ? "fleet memory + OEM manual + telemetry" : "OEM manual + telemetry, no memory"}
        </span>
      </header>
      <div ref={scroller} className="flex-1 space-y-4 overflow-y-auto px-4 py-4" aria-live="polite">
        {messages.length === 0 && !live && <EmptyPane copilot={copilot} />}
        {messages.map((m) =>
          m.kind === "user" ? (
            <div key={m.id} className="ml-auto max-w-[85%] rounded-xl rounded-br-sm bg-surface-2 px-3 py-2 text-sm">
              <div className="mb-0.5 text-[11px] font-medium text-ink-3">{m.technician}</div>
              {m.text}
            </div>
          ) : m.kind === "error" ? (
            <div key={m.id} className="flex gap-2 rounded-lg bg-bad-wash px-3 py-2 text-sm text-bad-ink">
              <Icon name="alert" className="mt-0.5 size-4 shrink-0" />
              {m.text}
            </div>
          ) : (
            <RunCard key={m.id} msgId={m.id} run={m.run} outcome={m.outcome} onConfirm={onConfirm} onInspect={onInspect} />
          ),
        )}
        {live && <LivePipeline live={live} copilot={copilot} />}
      </div>
    </div>
  );
}

function StepIcon({ state }: { state: StepState }) {
  if (state === "running")
    return <span className="mt-1 size-2.5 shrink-0 animate-pulse rounded-full bg-accent" aria-label="in progress" />;
  if (state === "warn") return <Icon name="alert" className="mt-0.5 size-3.5 shrink-0 text-warn-ink" />;
  if (state === "info") return <span className="mt-1 size-2.5 shrink-0 rounded-full border-2 border-ink-3" aria-hidden />;
  return <Icon name="check" className="mt-0.5 size-3.5 shrink-0 text-good-ink" />;
}

function LivePipeline({ live, copilot }: { live: Live; copilot: boolean }) {
  return (
    <div className="space-y-3">
      {live.steps.length === 0 ? (
        <Spinner label={copilot ? "Starting the memory pipeline…" : "Reading the OEM manual…"} />
      ) : (
        <ol className="space-y-1.5 rounded-lg border border-line bg-surface-2 p-3" aria-label="Pipeline progress">
          {live.steps.map((s) => (
            <li key={s.key} className="flex gap-2 text-xs">
              <StepIcon state={s.state} />
              <span>
                <span className="font-medium text-ink">{s.label}</span>
                {s.detail && <span className="text-ink-2"> · {s.detail}</span>}
              </span>
            </li>
          ))}
        </ol>
      )}
      {live.answer && <Markdown>{live.answer}</Markdown>}
    </div>
  );
}

function EmptyPane({ copilot }: { copilot: boolean }) {
  return (
    <div className="flex h-full flex-col justify-center gap-2 px-2 text-sm text-ink-2">
      {copilot ? (
        <>
          <p className="font-medium text-ink">Answers with what the fleet has learned.</p>
          <p>
            Before reasoning, it recalls past repairs of this model, error code and unit from Hindsight, checks which fixes
            actually held and who found them, verifies every citation, and retains the session so the next technician
            benefits.
          </p>
        </>
      ) : (
        <>
          <p className="font-medium text-ink">Answers from the OEM manual.</p>
          <p>Same model, same telemetry, but no memory of past jobs. This is the control.</p>
        </>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ run card
function RunCard({
  msgId,
  run,
  outcome,
  onConfirm,
  onInspect,
}: {
  msgId: string;
  run: AgentRun;
  outcome?: Outcome;
  onConfirm: (msgId: string, run: AgentRun, held: boolean) => void;
  onInspect: () => void;
}) {
  const mem = run.memory;
  const [copied, setCopied] = useState(false);
  const waited = run.llm.waits?.reduce((a, w) => a + w.seconds, 0) ?? 0;
  return (
    <article className="space-y-3">
      {mem && <RecallStrip run={run} onInspect={onInspect} />}
      {mem?.delta?.has_delta && <DeltaCard delta={mem.delta} />}
      <Markdown>{run.answer}</Markdown>
      {run.citations && run.mode === "copilot" && <CitationCheck report={run.citations} />}
      {run.warnings.length > 0 && (
        <ul className="space-y-1">
          {run.warnings.map((w) => (
            <li key={w} className="flex gap-1.5 text-xs text-warn-ink">
              <Icon name="alert" className="mt-0.5 size-3.5 shrink-0" />
              {w}
            </li>
          ))}
        </ul>
      )}
      {run.tool_calls.length > 0 && <ToolCalls calls={run.tool_calls} />}
      {run.ticket && run.mode === "copilot" && (
        <TicketCard run={run} outcome={outcome} onConfirm={(held) => onConfirm(msgId, run, held)} />
      )}
      <div className="tabular flex flex-wrap items-center gap-x-1 text-[11px] text-ink-3">
        <span>
          {run.llm.error ? "LLM offline" : `${run.llm.model} · ${run.llm.rounds} round${run.llm.rounds === 1 ? "" : "s"}`}
          {run.llm.usage?.total_tokens ? ` · ${run.llm.usage.total_tokens.toLocaleString()} tokens` : ""}
          {run.timings.recall_ms !== undefined && ` · recall ${run.timings.recall_ms} ms`}
          {` · total ${(run.timings.total_ms / 1000).toFixed(1)} s`}
          {waited > 0 && ` (incl. ${waited.toFixed(0)} s rate-limit pacing)`}
        </span>
        <button
          onClick={() => {
            void navigator.clipboard?.writeText(run.answer).then(() => {
              setCopied(true);
              window.setTimeout(() => setCopied(false), 1500);
            });
          }}
          className="ml-auto rounded px-1.5 py-0.5 text-ink-2 hover:bg-surface-2 hover:text-ink"
        >
          {copied ? "Copied" : "Copy answer"}
        </button>
      </div>
    </article>
  );
}

function CitationCheck({ report }: { report: CitationReport }) {
  if (report.total === 0)
    return <p className="text-xs text-ink-3">No memory citations in this answer.</p>;
  const ok = report.issues.length === 0;
  return (
    <details className="group rounded-lg border border-line text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 select-none">
        <Icon name="chevron" className="size-3 text-ink-2 transition-transform group-open:rotate-90" />
        {ok ? (
          <Badge tone="good">
            <Icon name="check" className="size-3" /> {report.verified}/{report.total} citations verified against the ledger
          </Badge>
        ) : (
          <Badge tone="warn">
            <Icon name="alert" className="size-3" /> {report.issues.length} citation{report.issues.length === 1 ? "" : "s"} don’t match the ledger
          </Badge>
        )}
      </summary>
      <ul className="space-y-1 border-t border-line px-3 py-2">
        {report.citations.map((c) => (
          <li key={`${c.technician}-${c.date}-${c.unit}`} className="flex flex-wrap items-center gap-1.5">
            {c.status === "verified" ? (
              <Icon name="check" className="size-3.5 text-good-ink" />
            ) : (
              <Icon name="alert" className="size-3.5 text-warn-ink" />
            )}
            <span className="font-mono text-[11px]">
              {c.technician} · {c.date}
              {c.unit ? ` · ${c.unit}` : ""}
            </span>
            {c.status === "unit_mismatch" && (
              <span className="text-warn-ink">ledger shows {c.ledger_units.join(", ")} for that technician and date</span>
            )}
            {c.status === "not_found" && <span className="text-warn-ink">no repair by this technician on that date</span>}
          </li>
        ))}
      </ul>
    </details>
  );
}

function RecallStrip({ run, onInspect }: { run: AgentRun; onInspect: () => void }) {
  const mem = run.memory!;
  const recallMs = Math.max(...mem.recalls.map((r) => r.latency_ms), 0);
  const fallback = mem.recalls.find((r) => r.source === "local_fallback");
  const cached = mem.recalls.some((r) => r.status === "cached");
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-xs">
      <Badge tone={statusTone(mem.source)} title={fallback?.error ?? undefined}>
        <Icon name="memory" className="size-3" />
        {mem.hits.length} memories · {mem.source === "hindsight" ? "Hindsight" : mem.source === "mixed" ? "mixed" : "local fallback"} ·{" "}
        {recallMs} ms{cached ? " · prefetched" : ""}
      </Badge>
      {mem.reflection && (
        <Badge tone={mem.reflection.source === "hindsight" ? "good" : "neutral"} title={mem.reflection.trigger}>
          <Icon name="spark" className="size-3" />
          {mem.reflection.source === "hindsight" ? `reflection${mem.reflection.cached ? " (cached)" : ""}` : "local synthesis"}
        </Badge>
      )}
      {mem.retain && (
        <Badge tone={mem.retain.source === "hindsight" ? "good" : "neutral"} title={mem.retain.error ?? undefined}>
          {mem.retain.source === "hindsight" ? `retained · ${mem.retain.status}` : "saved to outbox"}
        </Badge>
      )}
      <button onClick={onInspect} className="ml-auto text-xs font-medium text-accent-ink hover:underline">
        Inspect memory →
      </button>
    </div>
  );
}

function Meter({ held, attempts, tone }: { held: number; attempts: number; tone: "accent" | "neutral" }) {
  const share = attempts ? held / attempts : 0;
  return (
    <div
      className={`h-1.5 w-16 overflow-hidden rounded-full ${tone === "accent" ? "bg-accent-wash" : "bg-surface-3"}`}
      role="img"
      aria-label={`held ${held} of ${attempts}`}
    >
      <div className={`h-full rounded-full ${tone === "accent" ? "bg-accent" : "bg-ink-3"}`} style={{ width: `${share * 100}%` }} />
    </div>
  );
}

function DeltaCard({ delta }: { delta: MemoryDelta }) {
  const best = delta.field_fixes[0];
  return (
    <section className="rounded-lg border border-accent-line bg-accent-wash p-3" aria-label="Memory delta">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5">
        <span className="text-xs font-semibold tracking-wide text-accent-ink uppercase">Memory delta</span>
        <Badge tone="neutral">{delta.sample_size} outcomes</Badge>
        {delta.site_specific && delta.site && <Badge tone="accent">matched to {delta.site}</Badge>}
        <Badge tone={delta.source === "hindsight" ? "good" : "neutral"}>
          {delta.source === "hindsight" ? "stats: Hindsight" : "stats: local ledger"}
        </Badge>
      </div>
      <dl className="space-y-2 text-sm">
        {best && (
          <div className="grid grid-cols-[auto_1fr] items-start gap-x-3 gap-y-0.5">
            <dt className="pt-1.5">
              <Meter held={best.held} attempts={best.attempts} tone="accent" />
            </dt>
            <dd>
              <span className="tabular font-semibold">
                {best.held}/{best.attempts} held
              </span>{" "}
              <span className="text-ink-2">field fix:</span> {best.action}
              <div className="text-xs text-ink-2">
                First confirmed by <strong className="font-medium text-ink">{best.discovered_by}</strong> on{" "}
                {formatDate(best.discovered_on)} ({best.discovered_unit})
                {best.technicians.length > 1 && ` · applied by ${best.technicians.join(", ")}`}
              </div>
            </dd>
          </div>
        )}
        {delta.manual_action && (
          <div className="grid grid-cols-[auto_1fr] items-start gap-x-3">
            <dt className="pt-1.5">
              <Meter held={delta.manual_held} attempts={delta.manual_attempts} tone="neutral" />
            </dt>
            <dd>
              <span className="tabular font-semibold">
                {delta.manual_held}/{delta.manual_attempts} held
              </span>{" "}
              <span className="text-ink-2">OEM manual:</span> {delta.manual_action}
            </dd>
          </div>
        )}
      </dl>
      {delta.no_defect_checks.length > 0 && (
        <p className="mt-2 flex gap-1.5 text-xs text-ink-2">
          <Icon name="alert" className="mt-0.5 size-3.5 shrink-0 text-warn-ink" />
          Not always the cause: checked and ruled out on{" "}
          {delta.no_defect_checks.map((c) => `${c.unit_id} (${formatDate(c.date)})`).join(", ")}.
        </p>
      )}
    </section>
  );
}

const STAGE_LABEL: Record<string, string> = { prefetch: "prefetched", diagnosis: "model call", "work order": "work order" };

function ToolCalls({ calls }: { calls: ToolTrace[] }) {
  const repaired = calls.filter((c) => c.repaired).length;
  return (
    <details className="group rounded-lg border border-line">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs text-ink-2 select-none">
        <Icon name="chevron" className="size-3 transition-transform group-open:rotate-90" />
        <Icon name="wrench" className="size-3.5" />
        {calls.length} tool call{calls.length === 1 ? "" : "s"}
        {repaired > 0 && <Badge tone="warn">{repaired} auto-repaired</Badge>}
      </summary>
      <ul className="divide-y divide-line border-t border-line">
        {calls.map((c) => (
          <li key={c.id} className="space-y-1 px-3 py-2 text-xs">
            <div className="flex flex-wrap items-center gap-1.5">
              <code className="font-mono text-[11px] font-semibold text-ink">{c.name}</code>
              {c.stage && <Badge>{STAGE_LABEL[c.stage] ?? c.stage}</Badge>}
              <span className="tabular text-ink-3">{c.latency_ms} ms</span>
              {c.repaired && (
                <Badge tone="warn" title={c.repair_notes.join("\n")}>
                  repaired
                </Badge>
              )}
              {c.error && <Badge tone="bad">error</Badge>}
            </div>
            <div className="font-mono text-[11px] break-all text-ink-2">{JSON.stringify(c.arguments)}</div>
            {c.repaired && <div className="text-[11px] text-warn-ink">{c.repair_notes.join(" · ")}</div>}
            {c.error && <div className="text-[11px] text-bad-ink">{c.error}</div>}
          </li>
        ))}
      </ul>
    </details>
  );
}

function TicketCard({ run, outcome, onConfirm }: { run: AgentRun; outcome?: Outcome; onConfirm: (held: boolean) => void }) {
  const t = run.ticket!;
  return (
    <section className="rounded-lg border border-line bg-surface-2 p-3 text-sm" aria-label="Repair ticket">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="font-mono text-xs font-semibold">{t.id}</span>
        <Badge>{t.action_category === "field" ? "field fix" : "OEM step"}</Badge>
        <Badge>{t.priority}</Badge>
        {outcome?.state === "done" && <OutcomeBadge held={outcome.held ?? null} />}
      </div>
      <p className="mt-1 text-ink-2">
        <span className="text-ink">Recommended first:</span> {t.recommended_action}
      </p>
      {!outcome || outcome.state === "error" ? (
        <div className="mt-2.5 flex flex-wrap items-center gap-2">
          <span className="text-xs text-ink-2">After the repair, did it hold?</span>
          <button
            onClick={() => onConfirm(true)}
            className="flex h-8 items-center gap-1 rounded-md border border-line bg-surface px-3 text-xs font-medium hover:border-good hover:text-good-ink"
          >
            <Icon name="check" className="size-3.5" /> Fix held
          </button>
          <button
            onClick={() => onConfirm(false)}
            className="flex h-8 items-center gap-1 rounded-md border border-line bg-surface px-3 text-xs font-medium hover:border-bad hover:text-bad-ink"
          >
            <Icon name="x" className="size-3.5" /> Didn’t hold
          </button>
          {outcome?.state === "error" && <span className="text-xs text-bad-ink">{outcome.error}</span>}
        </div>
      ) : outcome.state === "saving" ? (
        <div className="mt-2">
          <Spinner label="Retaining outcome to memory…" />
        </div>
      ) : (
        <p className="mt-2 text-xs text-ink-2">
          Outcome {outcome.retain?.source === "hindsight"
            ? `retained to Hindsight (${outcome.retain.status}${outcome.retain.operation_id ? ` · ${outcome.retain.operation_id.slice(0, 8)}` : ""})`
            : "saved to the outbox; it syncs to Hindsight automatically"}
          . The next technician who hits {t.error_code} will see it.
        </p>
      )}
    </section>
  );
}
