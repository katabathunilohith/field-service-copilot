import type {
  AgentRun,
  Briefing,
  Bulletin,
  BulletinCandidate,
  Catalog,
  DiagnoseResponse,
  Directive,
  EvalReport,
  FleetMetrics,
  Health,
  MemoryEvent,
  Mode,
  OutboxStatus,
  NoteKind,
  OutcomeResponse,
  Reflection,
  SavedNote,
  StreamEvent,
  Transcription,
  ViewMode,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

// Optional shared access token (APP_ACCESS_TOKEN on the server). Kept for this tab only.
const TOKEN_KEY = "fsc-access-token";
let accessToken: string | null = null;
try {
  accessToken = sessionStorage.getItem(TOKEN_KEY);
} catch {
  /* storage unavailable: the token prompt will simply appear again */
}

export function setAccessToken(token: string) {
  accessToken = token;
  try {
    sessionStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* ignore */
  }
}

function headers(extra?: HeadersInit): HeadersInit {
  return { ...(accessToken ? { "X-Access-Token": accessToken } : {}), ...(extra ?? {}) };
}

const UNREACHABLE = "Cannot reach the Copilot API. Is `python main.py` running on port 8000?";

async function errorFrom(resp: Response): Promise<ApiError> {
  let detail = resp.statusText;
  try {
    const body = await resp.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
  } catch {
    /* non-JSON error body */
  }
  return new ApiError(detail || `HTTP ${resp.status}`, resp.status);
}

async function request<T>(path: string, init?: RequestInit & { json?: unknown }): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      ...init,
      body: init?.json !== undefined ? JSON.stringify(init.json) : init?.body,
      headers: headers(init?.json !== undefined ? { "Content-Type": "application/json" } : undefined),
    });
  } catch {
    throw new ApiError(UNREACHABLE, 0);
  }
  if (!resp.ok) throw await errorFrom(resp);
  return resp.json() as Promise<T>;
}

type History = Partial<Record<Mode, { role: "user" | "assistant"; content: string }[]>>;

/** POST a diagnosis and read the server-sent event stream; resolves with the final run. */
async function diagnoseStream(
  body: { query: string; technician_id: string; unit_id: string | null; mode: Mode; history: History },
  onEvent: (e: StreamEvent) => void,
): Promise<AgentRun> {
  let resp: Response;
  try {
    resp = await fetch("/api/diagnose/stream", {
      method: "POST",
      headers: headers({ "Content-Type": "application/json", Accept: "text/event-stream" }),
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError(UNREACHABLE, 0);
  }
  if (!resp.ok || !resp.body) throw await errorFrom(resp);

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let final: AgentRun | null = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) !== -1) {
      const chunk = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      let event = "message";
      const data: string[] = [];
      for (const line of chunk.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trim());
      }
      if (!data.length) continue; // keep-alive comment
      const parsed = { event, data: JSON.parse(data.join("\n")) } as StreamEvent;
      if (parsed.event === "error") throw new ApiError(parsed.data.message, 500);
      if (parsed.event === "done") final = parsed.data.run;
      onEvent(parsed);
    }
  }
  if (!final) throw new ApiError("The diagnosis stream ended unexpectedly.", 500);
  return final;
}

export const api = {
  health: () => request<Health>("/api/health"),
  catalog: () => request<Catalog>("/api/catalog"),
  metrics: () => request<FleetMetrics>("/api/metrics/fleet"),
  events: (limit = 150) => request<MemoryEvent[]>(`/api/memory/events?limit=${limit}`),
  diagnose: (body: { query: string; technician_id: string; unit_id: string | null; mode: ViewMode; history: History }) =>
    request<DiagnoseResponse>("/api/diagnose", { method: "POST", json: body }),
  diagnoseStream,
  prefetch: (query: string, technicianId: string, unitId: string | null) =>
    request<{ started: number; parsed?: AgentRun["parsed"] }>("/api/memory/prefetch", {
      method: "POST",
      json: { query, technician_id: technicianId, unit_id: unitId },
    }).catch(() => ({ started: 0, parsed: undefined })), // best effort: a failed prefetch only means a cold recall
  briefing: (unitId: string) => request<Briefing>(`/api/briefing?unit_id=${encodeURIComponent(unitId)}`),
  addNote: (body: { text: string; technician_id: string; unit_id: string; scope: "unit" | "site"; kind: NoteKind }) =>
    request<SavedNote>("/api/notes", { method: "POST", json: body }),
  transcribe: async (audio: Blob, filename: string): Promise<Transcription> => {
    const form = new FormData();
    form.append("audio", audio, filename);
    let resp: Response;
    try {
      resp = await fetch("/api/transcribe", { method: "POST", body: form, headers: headers() });
    } catch {
      throw new ApiError(UNREACHABLE, 0);
    }
    if (!resp.ok) throw await errorFrom(resp);
    return resp.json();
  },
  confirmOutcome: (ticketId: string, outcomeHeld: boolean, technicianId: string, notes = "") =>
    request<OutcomeResponse>(`/api/tickets/${encodeURIComponent(ticketId)}/outcome`, {
      method: "POST",
      json: { outcome_held: outcomeHeld, technician_id: technicianId, notes },
    }),
  reflect: (model: string, errorCode: string, force = true) =>
    request<Reflection>("/api/memory/reflect", { method: "POST", json: { model, error_code: errorCode, force } }),
  outbox: () => request<OutboxStatus>("/api/memory/outbox"),
  syncOutbox: () => request<OutboxStatus>("/api/memory/outbox/sync", { method: "POST" }),
  directives: () => request<{ source: string; items: Directive[]; error?: string }>("/api/memory/directives"),
  bulletins: () => request<{ candidates: BulletinCandidate[]; bulletins: Bulletin[] }>("/api/bulletins"),
  draftBulletin: (model: string, errorCode: string) =>
    request<Bulletin>("/api/bulletins/draft", { method: "POST", json: { model, error_code: errorCode } }),
  approveBulletin: (id: string, approver: string) =>
    request<Bulletin>(`/api/bulletins/${encodeURIComponent(id)}/approve`, { method: "POST", json: { approver } }),
  downloadBulletin: async (id: string) => {
    const resp = await fetch(`/api/bulletins/${encodeURIComponent(id)}/markdown`, { headers: headers() });
    if (!resp.ok) throw await errorFrom(resp);
    const url = URL.createObjectURL(await resp.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: `${id}.md` });
    a.click();
    URL.revokeObjectURL(url);
  },
  evalLatest: () => request<EvalReport>("/api/eval/latest"),
};

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "undated";
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00Z` : iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function pct(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return "–";
  return `${(value * 100).toFixed(digits)}%`;
}
