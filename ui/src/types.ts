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
    groq_baseline_model: string;
    voice_enabled: boolean;
    access_token_required: boolean;
    environment: string;
    setup_hints: string[];
  };
  hindsight: {
    reachable: boolean;
    reason?: string;
    latency_ms?: number;
    stats?: { total_nodes: number; total_documents: number; total_observations: number; pending_operations: number };
  };
  llm: { available: boolean; model: string; baseline_model: string; rate: Record<string, { limit_tpm: number; available_tokens: number }> };
  outbox: OutboxStatus;
  fallback_store_records: number;
  banks: string[];
}

export interface OutboxStatus {
  pending: number;
  enabled: boolean;
  oldest: string | null;
  pushed?: number;
  failed?: number;
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
  label: string;
  cache_age_s: number | null;
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
  work_order?: string | null;
  resolved_by?: string;
  resolution?: string;
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
  directives?: string[];
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

export type NoteKind = "site_rule" | "hazard" | "machine_quirk";

export interface FieldNote {
  id: string;
  kind: NoteKind;
  text: string;
  technician: string | null;
  role: string;
  date: string;
  unit_id: string | null;
  scope: "unit" | "site";
  site: string;
  source: "hindsight" | "syncing" | "local";
}

export interface Briefing {
  unit_id: string;
  site: string;
  items: FieldNote[];
  source: "hindsight" | "local_fallback";
  status: string;
  latency_ms: number;
}

export interface SavedNote {
  note: FieldNote;
  retain: RetainResult;
  redactions: string[];
}

export interface MemoryView {
  bank_id: string;
  source: "hindsight" | "local_fallback" | "mixed";
  recalls: RecallOutcome[];
  hits: MemoryHit[];
  delta: MemoryDelta | null;
  reflection: Reflection | null;
  briefing?: Briefing | null;
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
  result: Record<string, unknown> | null;
  prefetched?: boolean;
  stage?: "prefetch" | "diagnosis" | "work order";
}

export interface Citation {
  text: string;
  technician: string;
  date: string;
  unit: string | null;
  status: "verified" | "unit_mismatch" | "not_found";
  ledger_units: string[];
}

export interface CitationReport {
  total: number;
  verified: number;
  issues: Citation[];
  citations: Citation[];
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
  citations: CitationReport | null;
  ticket: Ticket | null;
  llm: {
    model: string;
    rounds: number;
    usage: Record<string, number>;
    recoveries: { round: number; kind: string }[];
    waits: { seconds: number; reason: string }[];
    error?: string;
  };
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

// ---------------------------------------------------------------- streaming
export type StreamEvent =
  | { event: "parsed"; data: { run_id: string; parsed: AgentRun["parsed"]; technician: Technician } }
  | { event: "tool"; data: Partial<ToolTrace> & { phase: "start" | "done"; name: string } }
  | { event: "recall"; data: { status: "start" | "done"; source?: string; count?: number; latency_ms?: number; recalls?: { label: string; source: string; status: string; latency_ms: number; hits: number }[]; delta?: MemoryDelta | null } }
  | { event: "reflect"; data: { status: string; source: string; cached: boolean; trigger: string } }
  | { event: "briefing"; data: { count: number; source: string; site: string; items: FieldNote[] } }
  | { event: "llm"; data: { round: number; status: string; model: string } }
  | { event: "llm_wait"; data: { seconds: number; reason: string } }
  | { event: "answer"; data: { text: string; citations: CitationReport | null } }
  | { event: "ticket"; data: { ticket: Ticket } }
  | { event: "retain"; data: { status: string; source: string; operation_id: string | null; error: string | null } }
  | { event: "done"; data: { run: AgentRun } }
  | { event: "error"; data: { message: string } };

export interface Transcription {
  text: string;
  duration_s: number | null;
  model: string;
  latency_ms: number;
  parsed: AgentRun["parsed"];
}

// ---------------------------------------------------------------- bulletins
export interface BulletinEvidence {
  records: number;
  period: [string, string] | null;
  oem_step: { action: string | null; attempts: number; held: number };
  field_fixes: {
    action: string;
    root_cause: string;
    attempts: number;
    held: number;
    first_confirmed_by: string | null;
    first_confirmed_on: string | null;
    first_unit: string | null;
    technicians: string[];
    sites: Record<string, number>;
  }[];
  ruled_out: OutcomeEntry[];
}

export interface Bulletin {
  id: string;
  status: "draft" | "approved";
  created_at: string;
  model_key: string;
  model_name: string;
  error_code: string;
  error_title: string;
  content: {
    title: string;
    symptom: string;
    root_cause: string;
    recommended_procedure: string[];
    when_to_use_oem_procedure: string;
    site_conditions?: string;
    confidence: "low" | "medium" | "high";
  };
  evidence: BulletinEvidence;
  generator: "hindsight_reflect" | "template";
  generator_error: string | null;
  directives_applied: string[];
  memories_consulted: number;
  approved_by?: string;
  approved_at?: string;
  publish?: { memory: string; directive: string };
}

export interface BulletinCandidate {
  model_key: string;
  model_name: string;
  error_code: string;
  error_title: string;
  evidence: BulletinEvidence;
  bulletin: { id: string; status: string } | null;
}

export interface Directive {
  id?: string;
  name: string;
  content: string;
  priority?: number;
  is_active?: boolean;
  tags?: string[] | null;
}

// ---------------------------------------------------------------- eval
export interface Rate {
  hits: number;
  total: number;
  rate: number | null;
}

export interface EvalRow {
  id: string;
  kind: "field" | "manual";
  unit: string;
  error_code: string;
  model_name: string;
  baseline: { root_cause_ok: boolean; first_fix_ok: boolean; safety_ok: boolean; first_fix: string; latency_ms: number };
  copilot: {
    root_cause_ok: boolean;
    first_fix_ok: boolean;
    safety_ok: boolean;
    first_fix: string;
    latency_ms: number;
    citations_total: number;
    citations_verified: number;
    citations_ok: boolean;
  };
  avoided_parts_usd: number;
  avoided_labor_h: number;
}

export interface EvalReport {
  available: boolean;
  generated_at?: string;
  model?: string;
  baseline_model?: string;
  memory?: string;
  summary?: {
    scenarios: number;
    baseline: Record<"root_cause_ok" | "first_fix_ok" | "safety_ok", Rate>;
    copilot: Record<"root_cause_ok" | "first_fix_ok" | "safety_ok" | "citations_ok", Rate>;
    tricky_first_fix: { baseline: Rate; copilot: Rate };
    control_first_fix: { baseline: Rate; copilot: Rate };
    avoided_parts_usd: number;
    avoided_labor_h: number;
  };
  rows?: EvalRow[];
}
