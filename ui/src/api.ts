import type {
  Catalog,
  DiagnoseResponse,
  FleetMetrics,
  Health,
  MemoryEvent,
  OutcomeResponse,
  Reflection,
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let resp: Response;
  try {
    resp = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    throw new ApiError("Cannot reach the Copilot API. Is `python main.py` running on port 8000?", 0);
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(detail || `HTTP ${resp.status}`, resp.status);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => request<Health>("/api/health"),
  catalog: () => request<Catalog>("/api/catalog"),
  metrics: () => request<FleetMetrics>("/api/metrics/fleet"),
  events: (limit = 150) => request<MemoryEvent[]>(`/api/memory/events?limit=${limit}`),
  diagnose: (body: {
    query: string;
    technician_id: string;
    unit_id: string | null;
    mode: ViewMode;
    history: Partial<Record<"baseline" | "copilot", { role: "user" | "assistant"; content: string }[]>>;
  }) => request<DiagnoseResponse>("/api/diagnose", { method: "POST", body: JSON.stringify(body) }),
  confirmOutcome: (ticketId: string, outcomeHeld: boolean, technicianId: string, notes = "") =>
    request<OutcomeResponse>(`/api/tickets/${encodeURIComponent(ticketId)}/outcome`, {
      method: "POST",
      body: JSON.stringify({ outcome_held: outcomeHeld, technician_id: technicianId, notes }),
    }),
  reflect: (model: string, errorCode: string, force = true) =>
    request<Reflection>("/api/memory/reflect", {
      method: "POST",
      body: JSON.stringify({ model, error_code: errorCode, force }),
    }),
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
