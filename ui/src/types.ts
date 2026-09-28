// Shapes returned by the FastAPI backend (see main.py and agent/orchestrator.py).

export type Mode = "baseline" | "copilot";
export type ViewMode = Mode | "compare";

export interface Technician {
  id: string;
  name: string;
  role: string;
  years_experience: number;
  specialties: string[];
}

export interface UnitSummary {
  id: string;
  model: string;
  model_name: string;
  site: string;
  location?: string;
}

export interface SamplePrompt {
  technician: string;
  unit: string;
  text: string;
}

export interface CatalogModel {
  key: string;
  name: string;
  fleet: string;
  category: string;
  error_codes: { code: string; title: string; severity: string }[];
}

export interface Catalog {
  fleet: string;
  technicians: Technician[];
  models: CatalogModel[];
  units: UnitSummary[];
  sample_prompts: SamplePrompt[];
}

export interface Health {
  status: string;
  config: {
    hindsight_configured: boolean;
    bank_id: string;
    per_fleet_banks: boolean;
    hindsight_timeout_s: number;
    groq_configured: boolean;
    groq_model: string;
    environment: string;
  };
  hindsight: { reachable: boolean; reason?: string; latency_ms?: number };
  llm: { available: boolean; model: string };
  fallback_store_records: number;
  banks: string[];
}

export interface MemoryHit {
  id: string;
  text: string;
  type: string;
  score: number;
  source: string;
  via: string;
  occurred_at: string | null;
  document_id: string | null;
  tags: string[];
  metadata: Record<string, string>;
  technician: string | null;
  unit_id: string | null;
  error_code: string | null;
  outcome_held: boolean | null;
  action_category: string | null;
  action_taken: string | null;
  record_type: string | null;
  when: string;
}

export interface RecallOutcome {
  source: "hindsight" | "local_fallback";
  status: string;
  latency_ms: number;
  event_id: number;
  error: string | null;
  hits: MemoryHit[];
}

export interface FieldFix {
  action: string;
  root_cause: string;
  attempts: number;
  held: number;
  discovered_by: string | null;
  discovered_on: string | null;
  discovered_unit: string | null;
  technicians: string[];
  sites: Record<string, number>;
}

export interface OutcomeEntry {
  date: string;
  technician: string | null;
  unit_id: string | null;
  action: string;
  held: boolean | null;
}

export interface MemoryDelta {
  has_delta: boolean;
  headline: string | null;
  manual_action: string | null;
  manual_attempts: number;
  manual_held: number;
  field_fixes: FieldFix[];
  no_defect_checks: OutcomeEntry[];
  unit_history: OutcomeEntry[];
  sample_size: number;
  source: string;
  site: string | null;
  site_specific: boolean;
}

export interface Reflection {
  source: string;
  status: string;
  text: string;
  based_on: { id: string | null; text: string; type: string | null; occurred_start?: string | null }[];
  latency_ms: number;
  event_id: number;
  cached: boolean;
  error: string | null;
  trigger?: string;
}

export interface RetainResult {
  status: string;
  source: string;
  event_id: number;
  items: number;
  operation_id: string | null;
  error: string | null;
  document_ids: string[];
  record?: Record<string, unknown>;
}

export interface MemoryView {
  bank_id: string;
  source: "hindsight" | "local_fallback" | "mixed";
  recalls: RecallOutcome[];
  hits: MemoryHit[];
  delta: MemoryDelta | null;
  reflection: Reflection | null;
  retain: RetainResult | null;
}

export interface ToolTrace {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  raw_arguments: string;
  repaired: boolean;
  repair_notes: string[];
  latency_ms: number;
  error: string | null;
  result: Record<string, unknown>;
}

export interface Ticket {
  id: string;
  created_at: string;
  status: string;
  unit_id: string;
  model: string;
  error_code: string;
  technician_id: string;
  diagnosis: string;
  recommended_action: string;
  canonical_action?: string | null;
  action_category: "manual" | "field";
  priority: string;
  outcome_held: boolean | null;
  created_via: string;
}

export interface ManualSection {
  model: string;
  error_code: string;
  title: string;
  severity: string;
  document: string;
  section: string;
  primary_action: string;
}

export interface AgentRun {
  run_id: string;
  mode: Mode;
  technician: Technician;
  parsed: {
    model_key: string | null;
    model_name: string | null;
    error_code: string | null;
    error_title: string | null;
    unit_id: string | null;
    site: string | null;
    technician_id: string | null;
  };
  manual: ManualSection | null;
  answer: string;
  memory: MemoryView | null;
  tool_calls: ToolTrace[];
  ticket: Ticket | null;
  llm: { model: string; rounds: number; usage: Record<string, number>; recoveries: { round: number; kind: string }[]; error?: string };
  timings: Record<string, number>;
  warnings: string[];
  created_at: string;
}

export interface DiagnoseResponse {
  mode: ViewMode;
  baseline?: AgentRun;
  copilot?: AgentRun;
}

export interface OutcomeResponse {
  ticket: Ticket;
  retain: RetainResult;
}

export interface MemoryEvent {
  id: number;
  op: "recall" | "retain" | "reflect" | "bank";
  bank_id: string;
  status: string;
  source: string;
  latency_ms: number;
  request: Record<string, unknown>;
  response: unknown;
  error: string | null;
  run_id: string | null;
  summary: string;
  ts: string;
}

export interface WeekMetric {
  week: number;
  label: string;
  start: string;
  end: string;
  jobs: number;
  first_time_fixes: number;
  ftf_rate: number;
  callbacks: number;
  memory_records: number;
  peer_assisted_jobs: number;
  field_fix_first_visits: number;
  labor_hours: number;
  parts_cost_usd: number;
}

export interface KnowledgeTransfer {
  knowledge_key: string;
  error_code: string;
  model_name: string;
  finding: string;
  fix: string;
  discovered_by: string;
  discovered_role: string;
  discovered_on: string;
  discovered_unit: string;
  reuses: { technician: string; date: string; unit_id: string; site: string; work_order: string; held: boolean; peer: boolean }[];
  reuse_count: number;
  reuse_held: number;
  beneficiaries: string[];
}

export interface FleetMetrics {
  weeks: WeekMetric[];
  summary: {
    ftf_start: number;
    ftf_end: number;
    ftf_delta_pts: number;
    total_jobs: number;
    memory_records_seeded: number;
    memory_records_live: number;
    callbacks_avoided_last_week: number;
    field_discoveries: number;
    peer_reuses: number;
  };
  knowledge_transfers: KnowledgeTransfer[];
  technicians: { technician: string; role: string; jobs: number; ftf_weeks_1_2: number; ftf_weeks_3_4: number }[];
  live: { tickets_open: number; tickets_confirmed: number; tickets_held: number };
  definition: string;
}
