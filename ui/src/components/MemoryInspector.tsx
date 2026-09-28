import { useCallback, useEffect, useRef, useState } from "react";
import { api, formatDate } from "../api";
import type { AgentRun, Catalog, Directive, MemoryEvent, MemoryHit, OutboxStatus, Reflection } from "../types";
import { Badge, Icon, JsonBlock, Markdown, OutcomeBadge, Spinner, statusTone } from "./ui";

type Tab = "recalled" | "retain" | "reflect" | "guardrails" | "raw";

const TABS: { id: Tab; label: string }[] = [
  { id: "recalled", label: "Recalled" },
  { id: "retain", label: "Retain log" },
  { id: "reflect", label: "Reflection" },
  { id: "guardrails", label: "Guardrails" },
  { id: "raw", label: "Raw" },
];

interface Props {
  open: boolean;
  onClose: () => void;
  run: AgentRun | null;
  catalog: Catalog | null;
  refreshKey: number;
}

export default function MemoryInspector({ open, onClose, run, catalog, refreshKey }: Props) {
  const [tab, setTab] = useState<Tab>("recalled");
  const [events, setEvents] = useState<MemoryEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const load = useCallback(async () => {
    try {
      setEvents(await api.events(200));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void load();
    // Deferred retains are retried in the background; keep the log current while visible.
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
  }, [open, load, refreshKey]);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;
  const retains = events.filter((e) => e.op === "retain");

  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/30 lg:hidden" onClick={onClose} aria-hidden />
      <aside
        role="dialog"
        aria-label="Memory inspector"
        className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[34rem] flex-col border-l border-line bg-surface shadow-panel"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-3">
          <Icon name="memory" className="size-4 text-accent-ink" />
          <h2 className="text-sm font-semibold">Memory inspector</h2>
          {run?.memory && <Badge>bank {run.memory.bank_id}</Badge>}
          <button
            ref={closeRef}
            onClick={onClose}
            className="ml-auto rounded-md p-1.5 text-ink-2 hover:bg-surface-2 hover:text-ink"
            aria-label="Close memory inspector"
          >
            <Icon name="x" />
          </button>
        </header>
        <nav className="flex gap-1 border-b border-line px-3" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.id}
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => setTab(t.id)}
              className={`-mb-px border-b-2 px-2.5 py-2 text-sm ${
                tab === t.id ? "border-accent font-medium text-ink" : "border-transparent text-ink-2 hover:text-ink"
              }`}
            >
              {t.label}
              {t.id === "retain" && retains.length > 0 && <span className="tabular ml-1 text-xs text-ink-3">{retains.length}</span>}
            </button>
          ))}
        </nav>
        <div className="flex-1 overflow-y-auto p-4" role="tabpanel">
          {error && <p className="mb-3 rounded-md bg-bad-wash px-3 py-2 text-sm text-bad-ink">{error}</p>}
          {tab === "recalled" && <Recalled run={run} />}
          {tab === "retain" && <RetainLog events={retains} onSynced={load} />}
          {tab === "reflect" && <ReflectionPanel key={run?.run_id ?? "none"} run={run} catalog={catalog} />}
          {tab === "guardrails" && <Guardrails />}
          {tab === "raw" && <RawEvents events={events} />}
        </div>
      </aside>
    </>
  );
}

// ---------------------------------------------------------------- recalled
function Recalled({ run }: { run: AgentRun | null }) {
  if (!run?.memory) return <Empty text="Run a Copilot diagnosis to see what it recalled from Hindsight." />;
  const mem = run.memory;
  return (
    <div className="space-y-4">
      <section className="space-y-1.5">
        <h3 className="text-xs font-semibold tracking-wide text-ink-2 uppercase">Recall calls · run {run.run_id}</h3>
        {mem.recalls.map((r) => (
          <div key={r.event_id} className="flex flex-wrap items-center gap-1.5 text-xs">
            <span className="font-medium text-ink">{r.label || "recall"}</span>
            <Badge tone={statusTone(r.source)}>{r.source === "hindsight" ? "Hindsight" : "local fallback"}</Badge>
            <Badge tone={statusTone(r.status)}>{r.status === "cached" ? "prefetched cache" : r.status}</Badge>
            <span className="tabular text-ink-2">
              {r.hits.length} hits · {r.latency_ms} ms
              {r.cache_age_s !== null && r.cache_age_s !== undefined ? ` · fetched ${Math.round(r.cache_age_s)}s earlier` : ""}
            </span>
            {r.error && <span className="w-full text-warn-ink">{r.error}</span>}
          </div>
        ))}
      </section>
      <section className="space-y-2">
        <h3 className="text-xs font-semibold tracking-wide text-ink-2 uppercase">Memories in the prompt ({mem.hits.length})</h3>
        {mem.hits.length === 0 && <Empty text="Nothing relevant was recalled." />}
        {mem.hits.map((h, i) => (
          <HitCard key={`${h.id}-${i}`} hit={h} index={i + 1} />
        ))}
      </section>
    </div>
  );
}

function HitCard({ hit, index }: { hit: MemoryHit; index: number }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <article className="rounded-lg border border-line p-3">
      <div className="mb-1.5 flex flex-wrap items-center gap-1.5 text-xs">
        <span className="tabular font-mono text-[11px] text-ink-3">M{index}</span>
        <Badge tone={hit.type === "observation" ? "accent" : "neutral"}>{hit.type}</Badge>
        {hit.outcome_held !== null && <OutcomeBadge held={hit.outcome_held} />}
        {hit.action_category && <Badge>{hit.action_category === "field" ? "field fix" : hit.action_category === "manual" ? "OEM step" : hit.action_category}</Badge>}
        <span className="ml-auto flex items-center gap-1.5 text-ink-2" title="Recall score (final rank)">
          <span className="h-1 w-10 overflow-hidden rounded-full bg-accent-wash">
            <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.min(1, hit.score) * 100}%` }} />
          </span>
          <span className="tabular">{hit.score.toFixed(2)}</span>
        </span>
      </div>
      <div className="mb-1.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-ink-2">
        <span>{formatDate(hit.when || hit.occurred_at)}</span>
        {hit.technician && <span className="font-medium text-ink">{hit.technician}</span>}
        {hit.unit_id && <span>{hit.unit_id}</span>}
        {hit.via && <span className="text-ink-3">via {hit.via}</span>}
      </div>
      <p className={`text-sm leading-snug text-ink ${expanded ? "" : "line-clamp-3"}`}>{hit.text}</p>
      <button onClick={() => setExpanded((v) => !v)} className="mt-1 text-xs text-accent-ink hover:underline">
        {expanded ? "Show less" : "Show more"}
      </button>
      {expanded && hit.tags.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {hit.tags.map((t) => (
            <code key={t} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-ink-2">
              {t}
            </code>
          ))}
        </div>
      )}
    </article>
  );
}

// ---------------------------------------------------------------- retain
function OutboxBar({ onSynced }: { onSynced: () => void }) {
  const [status, setStatus] = useState<OutboxStatus | null>(null);
  const [syncing, setSyncing] = useState(false);
  useEffect(() => {
    api.outbox().then(setStatus).catch(() => setStatus(null));
  }, []);
  if (!status) return null;
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface-2 px-3 py-2 text-xs">
      <Icon name="refresh" className="size-3.5 text-ink-2" />
      <span className="text-ink-2">
        Outbox: <strong className="font-medium text-ink">{status.pending}</strong> record{status.pending === 1 ? "" : "s"} waiting for
        Hindsight{status.pending ? "" : " (everything is synced)"}
      </span>
      {status.pending > 0 && status.enabled && (
        <button
          disabled={syncing}
          onClick={async () => {
            setSyncing(true);
            try {
              setStatus(await api.syncOutbox());
              onSynced();
            } finally {
              setSyncing(false);
            }
          }}
          className="ml-auto rounded-md border border-line bg-surface px-2 py-1 font-medium hover:bg-surface-3 disabled:opacity-50"
        >
          {syncing ? "Syncing…" : "Sync now"}
        </button>
      )}
    </div>
  );
}

function RetainLog({ events, onSynced }: { events: MemoryEvent[]; onSynced: () => void }) {
  if (events.length === 0)
    return (
      <>
        <OutboxBar onSynced={onSynced} />
        <Empty text="Nothing retained yet this session. Each Copilot diagnosis and each confirmed outcome is retained." />
      </>
    );
  return (
    <>
    <OutboxBar onSynced={onSynced} />
    <ol className="space-y-2">
      {events.map((e) => {
        const items = (e.request.items as { content?: string; tags?: string[] }[] | undefined) ?? [];
        return (
          <li key={e.id} className="space-y-2 rounded-lg border border-line p-3">
            <div className="flex flex-wrap items-center gap-1.5 text-xs">
              <Badge tone={statusTone(e.status)}>{e.status}</Badge>
              <Badge tone={statusTone(e.source)}>{e.source === "hindsight" ? "Hindsight" : "local journal"}</Badge>
              <span className="tabular text-ink-3">{new Date(e.ts).toLocaleTimeString()}</span>
              <span className="tabular text-ink-3">· {e.latency_ms} ms</span>
            </div>
            <p className="text-sm">{e.summary}</p>
            {items[0]?.content && <p className="line-clamp-3 text-xs text-ink-2">{items[0].content}</p>}
            {e.error && <p className="text-xs text-warn-ink">{e.error}</p>}
            <JsonBlock label="Retain payload" value={e.request} />
            {e.response !== null && e.response !== undefined && <JsonBlock label="Response" value={e.response} />}
          </li>
        );
      })}
    </ol>
    </>
  );
}

// ---------------------------------------------------------------- guardrails
function Guardrails() {
  const [data, setData] = useState<{ source: string; items: Directive[]; error?: string } | null>(null);
  useEffect(() => {
    api.directives().then(setData).catch(() => setData({ source: "error", items: [] }));
  }, []);
  if (!data) return <Spinner label="Loading directives…" />;
  const sorted = [...data.items].sort((a, b) => (b.priority ?? 0) - (a.priority ?? 0));
  return (
    <div className="space-y-3">
      <p className="text-sm text-ink-2">
        Directives are standing rules Hindsight applies to every reflection on this bank. Global rules enforce safety and
        evidence; approved field bulletins add rules scoped to one model and error code.
      </p>
      {data.source !== "hindsight" && (
        <p className="text-xs text-warn-ink">Showing the configured defaults; Hindsight was not reachable{data.error ? ` (${data.error})` : ""}.</p>
      )}
      <ol className="space-y-2">
        {sorted.map((d) => (
          <li key={d.name} className="rounded-lg border border-line p-3">
            <div className="mb-1 flex flex-wrap items-center gap-1.5 text-xs">
              <Icon name="shield" className="size-3.5 text-accent-ink" />
              <code className="font-mono text-[11px] font-semibold">{d.name}</code>
              <Badge>priority {d.priority ?? 0}</Badge>
              {d.tags && d.tags.length > 0 ? (
                <Badge tone="accent">scoped: {d.tags.join(" + ")}</Badge>
              ) : (
                <Badge>global</Badge>
              )}
              {d.is_active === false && <Badge tone="warn">inactive</Badge>}
            </div>
            <p className="text-sm text-ink">{d.content}</p>
          </li>
        ))}
      </ol>
    </div>
  );
}

// ---------------------------------------------------------------- reflect
function ReflectionPanel({ run, catalog }: { run: AgentRun | null; catalog: Catalog | null }) {
  const [model, setModel] = useState(run?.parsed.model_key ?? catalog?.models[0]?.key ?? "");
  const [code, setCode] = useState(run?.parsed.error_code ?? "");
  const [result, setResult] = useState<Reflection | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const codes = catalog?.models.find((m) => m.key === model)?.error_codes ?? [];
  const shown = result ?? run?.memory?.reflection ?? null;

  async function synthesize() {
    setLoading(true);
    setError(null);
    try {
      setResult(await api.reflect(model, code || codes[0]?.code || ""));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-2">
        Reflection asks Hindsight to synthesize patterns across every retained repair for a model and error code: which OEM
        steps keep failing, which field fixes hold, and whether site conditions change the answer. The Copilot triggers it
        automatically when memory contradicts the manual.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-2">
          Model
          <select
            value={model}
            onChange={(e) => {
              setModel(e.target.value);
              setCode("");
            }}
            className="h-8 rounded-md border border-line bg-surface px-2 text-sm text-ink"
          >
            {catalog?.models.map((m) => (
              <option key={m.key} value={m.key}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs font-medium text-ink-2">
          Error code
          <select
            value={code || codes[0]?.code || ""}
            onChange={(e) => setCode(e.target.value)}
            className="h-8 rounded-md border border-line bg-surface px-2 text-sm text-ink"
          >
            {codes.map((c) => (
              <option key={c.code} value={c.code}>
                {c.code} · {c.title}
              </option>
            ))}
          </select>
        </label>
        <button
          onClick={synthesize}
          disabled={loading || !model}
          className="flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-sm font-medium text-white hover:brightness-110 disabled:opacity-50"
        >
          <Icon name="spark" className="size-3.5" />
          Synthesize now
        </button>
      </div>
      {loading && <Spinner label="Hindsight is reflecting over retained memories…" />}
      {error && <p className="rounded-md bg-bad-wash px-3 py-2 text-sm text-bad-ink">{error}</p>}
      {shown ? (
        <section className="space-y-2 rounded-lg border border-line p-3">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <Badge tone={statusTone(shown.source)}>{shown.source === "hindsight" ? "Hindsight reflect" : "local synthesis"}</Badge>
            {shown.cached && <Badge>cached</Badge>}
            <span className="tabular text-ink-3">{shown.latency_ms} ms</span>
            {shown.trigger && <span className="text-ink-3">· trigger: {shown.trigger}</span>}
          </div>
          {shown.error && <p className="text-xs text-warn-ink">{shown.error}</p>}
          {shown.directives && shown.directives.length > 0 && (
            <p className="text-xs text-ink-2">Directives applied: {shown.directives.join(", ")}</p>
          )}
          <Markdown>{shown.text}</Markdown>
          {shown.based_on.length > 0 && <JsonBlock label={`Based on ${shown.based_on.length} memories`} value={shown.based_on} />}
        </section>
      ) : (
        !loading && <Empty text="No reflection yet. Pick a model and error code, then synthesize." />
      )}
    </div>
  );
}

// ---------------------------------------------------------------- raw
function RawEvents({ events }: { events: MemoryEvent[] }) {
  if (events.length === 0) return <Empty text="No memory operations yet." />;
  return (
    <ol className="space-y-2">
      {events.map((e) => (
        <li key={e.id} className="space-y-1.5 rounded-lg border border-line p-3">
          <div className="flex flex-wrap items-center gap-1.5 text-xs">
            <code className="font-mono text-[11px] font-semibold">{e.op}</code>
            <Badge tone={statusTone(e.status)}>{e.status}</Badge>
            <Badge tone={statusTone(e.source)}>{e.source}</Badge>
            <span className="tabular text-ink-3">
              {e.latency_ms} ms · {new Date(e.ts).toLocaleTimeString()}
            </span>
          </div>
          <p className="text-xs text-ink-2">{e.summary}</p>
          {e.error && <p className="text-xs text-warn-ink">{e.error}</p>}
          <JsonBlock label="Request" value={e.request} />
          {e.response !== null && e.response !== undefined && <JsonBlock label="Response" value={e.response} />}
        </li>
      ))}
    </ol>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-ink-2">{text}</p>;
}
