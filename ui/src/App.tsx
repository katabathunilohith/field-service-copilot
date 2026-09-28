import { useCallback, useEffect, useState } from "react";
import { api, ApiError, setAccessToken } from "./api";
import BulletinsPanel from "./components/BulletinsPanel";
import ChatWindow from "./components/ChatWindow";
import LearningMetrics from "./components/LearningMetrics";
import MemoryInspector from "./components/MemoryInspector";
import { Badge, Icon } from "./components/ui";
import type { AgentRun, Catalog, Health } from "./types";

type Tab = "diagnose" | "fleet" | "bulletins";
type Theme = "system" | "light" | "dark";

function readTheme(): Theme {
  try {
    const stored = localStorage.getItem("fsc-theme");
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

export default function App() {
  const [tab, setTab] = useState<Tab>("diagnose");
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [apiError, setApiError] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [lastRun, setLastRun] = useState<AgentRun | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [theme, setTheme] = useState<Theme>(readTheme);
  const [needsToken, setNeedsToken] = useState(false);
  const [tokenInput, setTokenInput] = useState("");
  const [loadKey, setLoadKey] = useState(0);

  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") delete root.dataset.theme;
    else root.dataset.theme = theme;
    try {
      localStorage.setItem("fsc-theme", theme);
    } catch {
      /* storage unavailable: theme still applies for this visit */
    }
  }, [theme]);

  useEffect(() => {
    const fail = (e: unknown) => {
      if (e instanceof ApiError && e.status === 401) setNeedsToken(true);
      else setApiError(e instanceof Error ? e.message : String(e));
    };
    api.catalog().then(setCatalog).catch(fail);
    const poll = () =>
      api
        .health()
        .then((h) => {
          setHealth(h);
          setApiError(null);
        })
        .catch(fail);
    poll();
    const timer = window.setInterval(poll, 20000);
    return () => window.clearInterval(timer);
  }, [loadKey]);

  const bump = useCallback(() => setRefreshKey((k) => k + 1), []);
  const closeInspector = useCallback(() => setInspectorOpen(false), []);

  const memoryOk = health?.hindsight.reachable;
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="z-20 border-b border-line bg-surface/90 backdrop-blur lg:sticky lg:top-0">
        <div className="mx-auto flex max-w-[1500px] flex-wrap items-center gap-x-5 gap-y-2 px-4 py-2.5">
          <div className="flex items-center gap-2.5">
            <span className="grid size-8 place-items-center rounded-lg bg-accent text-white" aria-hidden>
              <Icon name="wrench" className="size-4.5" />
            </span>
            <div className="leading-tight">
              <h1 className="text-sm font-semibold">Field Service Copilot</h1>
              <p className="text-[11px] text-ink-3">{catalog?.fleet ?? "Commercial HVAC · Solar · Vertical transport"}</p>
            </div>
          </div>

          <nav className="flex gap-1 rounded-lg bg-surface-2 p-0.5" aria-label="Views">
            {(
              [
                ["diagnose", "Diagnostic console", "wrench"],
                ["fleet", "Fleet learning", "chart"],
                ["bulletins", "Field bulletins", "doc"],
              ] as const
            ).map(([id, label, icon]) => (
              <button
                key={id}
                onClick={() => setTab(id)}
                aria-current={tab === id ? "page" : undefined}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm ${
                  tab === id ? "bg-surface font-medium text-ink shadow-sm" : "text-ink-2 hover:text-ink"
                }`}
              >
                <Icon name={icon} className="size-3.5" />
                {label}
              </button>
            ))}
          </nav>

          <div className="ml-auto flex flex-wrap items-center gap-2">
            {health && (
              <>
                <Badge
                  tone={memoryOk ? "good" : "warn"}
                  title={memoryOk ? `Hindsight reachable (${health.hindsight.latency_ms} ms)` : health.hindsight.reason}
                >
                  <Icon name="memory" className="size-3" />
                  {memoryOk
                    ? `Hindsight · ${health.config.bank_id}${health.hindsight.stats ? ` · ${health.hindsight.stats.total_nodes} facts` : ""}`
                    : "Memory: local fallback"}
                </Badge>
                {health.outbox.pending > 0 && (
                  <Badge tone="neutral" title="Records saved locally that will sync to Hindsight automatically">
                    <Icon name="refresh" className="size-3" />
                    {health.outbox.pending} syncing
                  </Badge>
                )}
                <Badge tone={health.llm.available ? "good" : "warn"} title={health.llm.model}>
                  {health.llm.available ? health.llm.model : "LLM not configured"}
                </Badge>
              </>
            )}
            <button
              onClick={() => setInspectorOpen((v) => !v)}
              aria-expanded={inspectorOpen}
              className={`flex h-8 items-center gap-1.5 rounded-md border px-3 text-sm ${
                inspectorOpen ? "border-accent-line bg-accent-wash text-accent-ink" : "border-line text-ink-2 hover:bg-surface-2 hover:text-ink"
              }`}
            >
              <Icon name="memory" className="size-3.5" />
              Memory inspector
            </button>
            <button
              onClick={() => setTheme((t) => (t === "system" ? "light" : t === "light" ? "dark" : "system"))}
              className="flex h-8 items-center gap-1 rounded-md border border-line px-2 text-xs text-ink-2 hover:bg-surface-2"
              aria-label={`Theme: ${theme}. Click to change.`}
              title={`Theme: ${theme}`}
            >
              <Icon name={theme === "dark" ? "moon" : "sun"} className="size-3.5" />
              {theme === "system" ? "Auto" : theme === "light" ? "Light" : "Dark"}
            </button>
          </div>
        </div>
      </header>

      {apiError && (
        <div className="mx-auto mt-3 w-full max-w-[1500px] px-4">
          <p className="flex items-center gap-2 rounded-lg bg-bad-wash px-3 py-2 text-sm text-bad-ink" role="alert">
            <Icon name="alert" /> {apiError}
          </p>
        </div>
      )}
      {health && health.config.setup_hints.length > 0 && (
        <div className="mx-auto mt-3 w-full max-w-[1500px] px-4">
          <div className="rounded-lg bg-warn-wash px-3 py-2 text-sm text-warn-ink" role="status">
            <p className="flex items-center gap-2 font-medium">
              <Icon name="alert" /> Setup needed. The app is running in degraded mode:
            </p>
            <ul className="mt-1 list-disc pl-9 text-xs">
              {health.config.setup_hints.map((h) => (
                <li key={h}>{h}</li>
              ))}
            </ul>
          </div>
        </div>
      )}
      {needsToken && (
        <div className="mx-auto mt-3 w-full max-w-[1500px] px-4">
          <form
            className="flex flex-wrap items-center gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-sm"
            onSubmit={(e) => {
              e.preventDefault();
              setAccessToken(tokenInput.trim());
              setNeedsToken(false);
              setLoadKey((k) => k + 1);
            }}
          >
            <Icon name="shield" className="text-accent-ink" />
            <label htmlFor="token">This deployment requires an access token:</label>
            <input
              id="token"
              type="password"
              value={tokenInput}
              onChange={(e) => setTokenInput(e.target.value)}
              className="h-8 min-w-60 rounded-md border border-line bg-surface px-2"
              autoComplete="off"
            />
            <button className="h-8 rounded-md bg-accent px-3 font-medium text-white" type="submit">
              Unlock
            </button>
          </form>
        </div>
      )}

      <main className="mx-auto flex w-full max-w-[1500px] flex-1 flex-col px-4 py-4">
        <div hidden={tab !== "diagnose"} className="flex min-h-0 flex-1 flex-col lg:h-[calc(100dvh-5.5rem)]">
          <ChatWindow
            catalog={catalog}
            voiceEnabled={!!health?.config.voice_enabled}
            onRun={setLastRun}
            onMemoryChanged={bump}
            onInspect={() => setInspectorOpen(true)}
          />
        </div>
        {tab === "fleet" && <LearningMetrics refreshKey={refreshKey} />}
        {tab === "bulletins" && <BulletinsPanel onPublished={bump} />}
      </main>

      <MemoryInspector open={inspectorOpen} onClose={closeInspector} run={lastRun} catalog={catalog} refreshKey={refreshKey} />
    </div>
  );
}
