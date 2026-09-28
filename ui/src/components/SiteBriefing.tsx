import { useEffect, useState } from "react";
import { api, ApiError, formatDate } from "../api";
import type { Briefing, FieldNote, NoteKind } from "../types";
import { Badge, Icon } from "./ui";
import VoiceButton from "./VoiceButton";

const KIND: Record<NoteKind, { label: string; tone: "warn" | "accent" | "neutral" }> = {
  hazard: { label: "Hazard", tone: "warn" },
  site_rule: { label: "Site rule", tone: "accent" },
  machine_quirk: { label: "Equipment quirk", tone: "neutral" },
};

interface Props {
  unitId: string;
  technicianId: string;
  voiceEnabled: boolean;
  refreshKey: number;
  onSaved: () => void;
}

/** Tribal knowledge for the unit in view: what other technicians noted about this machine and site. */
export default function SiteBriefing({ unitId, technicianId, voiceEnabled, refreshKey, onSaved }: Props) {
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setBriefing(null);
    setError(null);
    api
      .briefing(unitId)
      .then((b) => !cancelled && setBriefing(b))
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [unitId, refreshKey]);

  const items = briefing?.items ?? [];
  const shown = expanded ? items : items.slice(0, 2);
  const counts = (["hazard", "site_rule", "machine_quirk"] as NoteKind[])
    .map((k) => [k, items.filter((i) => i.kind === k).length] as const)
    .filter(([, n]) => n > 0);

  return (
    <section className="rounded-xl border border-line bg-surface px-3 py-2.5 shadow-panel" aria-label={`Briefing for ${unitId}`}>
      <div className="flex flex-wrap items-center gap-2">
        <Icon name="shield" className="size-4 text-accent-ink" />
        <h3 className="text-sm font-semibold">Before you go</h3>
        <span className="text-xs text-ink-2">
          {unitId}
          {briefing ? ` · ${briefing.site}` : ""}
        </span>
        {counts.map(([k, n]) => (
          <Badge key={k} tone={KIND[k].tone}>
            {n} {KIND[k].label.toLowerCase()}
            {n > 1 ? "s" : ""}
          </Badge>
        ))}
        {briefing && items.length > 0 && (
          <Badge tone={briefing.source === "hindsight" ? "good" : "neutral"}>
            {briefing.source === "hindsight" ? "from Hindsight" : "local memory"}
          </Badge>
        )}
        <div className="ml-auto flex items-center gap-2">
          {items.length > 2 && (
            <button onClick={() => setExpanded((v) => !v)} className="text-xs font-medium text-accent-ink hover:underline">
              {expanded ? "Show less" : `Show all ${items.length}`}
            </button>
          )}
          <button
            onClick={() => setAdding((v) => !v)}
            aria-expanded={adding}
            className="rounded-md border border-line px-2.5 py-1 text-xs font-medium text-ink-2 hover:bg-surface-2 hover:text-ink"
          >
            {adding ? "Close" : "+ Add field note"}
          </button>
        </div>
      </div>

      {error && <p className="mt-1.5 text-xs text-bad-ink">{error}</p>}
      {!briefing && !error && <p className="mt-1.5 text-xs text-ink-3">Recalling field notes…</p>}
      {briefing && items.length === 0 && !adding && (
        <p className="mt-1.5 text-xs text-ink-2">
          No field notes for {unitId} or {briefing.site} yet. Add the first site rule, hazard or equipment quirk so the next
          technician knows before they arrive.
        </p>
      )}
      {shown.length > 0 && (
        <ul className="mt-2 space-y-1.5">
          {shown.map((n) => (
            <NoteRow key={n.id} note={n} />
          ))}
        </ul>
      )}
      {adding && (
        <NoteForm
          unitId={unitId}
          site={briefing?.site}
          technicianId={technicianId}
          voiceEnabled={voiceEnabled}
          onSaved={() => {
            setExpanded(true);
            onSaved();
          }}
        />
      )}
    </section>
  );
}

function NoteRow({ note }: { note: FieldNote }) {
  const k = KIND[note.kind] ?? KIND.machine_quirk;
  return (
    <li className="flex flex-wrap items-start gap-x-2 gap-y-0.5 text-sm">
      <Badge tone={k.tone}>
        {note.kind === "hazard" && <Icon name="alert" className="size-3" />}
        {k.label}
      </Badge>
      <span className="min-w-0 flex-1 text-ink">{note.text}</span>
      <span className="text-xs text-ink-3">
        {note.technician} · {formatDate(note.date)} · {note.scope === "site" ? "whole site" : `this unit`}
        {note.source === "syncing" && " · syncing to Hindsight"}
      </span>
    </li>
  );
}

function NoteForm({
  unitId,
  site,
  technicianId,
  voiceEnabled,
  onSaved,
}: {
  unitId: string;
  site?: string;
  technicianId: string;
  voiceEnabled: boolean;
  onSaved: () => void;
}) {
  const [kind, setKind] = useState<NoteKind>("site_rule");
  const [scope, setScope] = useState<"unit" | "site">("site");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; message: string } | null>(null);

  function pickKind(k: NoteKind) {
    setKind(k);
    setScope(k === "machine_quirk" ? "unit" : "site"); // quirks belong to a machine; rules and hazards to the site
  }

  async function save() {
    setBusy(true);
    setResult(null);
    try {
      const saved = await api.addNote({ text: text.trim(), technician_id: technicianId, unit_id: unitId, scope, kind });
      const where = scope === "site" ? `every technician at ${site ?? "this site"}` : `every technician on ${unitId}`;
      const retained = saved.retain.source === "hindsight" ? "Retained to Hindsight" : "Saved; it syncs to Hindsight automatically";
      const removed = saved.redactions.length ? ` Removed before saving: ${saved.redactions.join(", ")}.` : "";
      setResult({ ok: true, message: `${retained}. Shared with ${where}.${removed}` });
      setText("");
      onSaved();
    } catch (err) {
      setResult({ ok: false, message: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="mt-2.5 space-y-2 border-t border-line pt-2.5"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <div role="radiogroup" aria-label="Note type" className="flex rounded-md border border-line bg-surface-2 p-0.5">
          {(Object.keys(KIND) as NoteKind[]).map((k) => (
            <button
              type="button"
              key={k}
              role="radio"
              aria-checked={kind === k}
              onClick={() => pickKind(k)}
              className={`rounded px-2.5 py-1 ${kind === k ? "bg-surface font-medium text-ink shadow-sm" : "text-ink-2 hover:text-ink"}`}
            >
              {KIND[k].label}
            </button>
          ))}
        </div>
        <div role="radiogroup" aria-label="Applies to" className="flex rounded-md border border-line bg-surface-2 p-0.5">
          {(["unit", "site"] as const).map((s) => (
            <button
              type="button"
              key={s}
              role="radio"
              aria-checked={scope === s}
              onClick={() => setScope(s)}
              className={`rounded px-2.5 py-1 ${scope === s ? "bg-surface font-medium text-ink shadow-sm" : "text-ink-2 hover:text-ink"}`}
            >
              {s === "unit" ? `This unit (${unitId})` : "Whole site"}
            </button>
          ))}
        </div>
      </div>
      <div className="flex items-end gap-2">
        <label htmlFor="note-text" className="sr-only">
          Field note
        </label>
        <textarea
          id="note-text"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={2}
          maxLength={500}
          placeholder={
            kind === "hazard"
              ? "e.g. Roof walkway is slippery after rain; use the east ladder."
              : kind === "site_rule"
                ? "e.g. Tower B roof needs a facilities escort after 6pm."
                : "e.g. VFD cabinet hinge is seized; bring a 10 mm socket."
          }
          className="min-h-11 flex-1 resize-none rounded-lg border border-line bg-surface px-2 py-1.5 text-sm text-ink placeholder:text-ink-3"
        />
        {voiceEnabled && (
          <VoiceButton
            disabled={busy}
            onTranscript={(t) => setText((prev) => (prev.trim() ? `${prev.trim()} ${t.text}` : t.text))}
            onError={(m) => setResult({ ok: false, message: m })}
          />
        )}
        <button
          type="submit"
          disabled={busy || text.trim().length < 8}
          className="h-10 rounded-lg bg-accent px-3 text-sm font-medium text-white hover:brightness-110 disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save note"}
        </button>
      </div>
      <p className="text-[11px] text-ink-3">
        Shared with every technician. Access codes, passwords, phone numbers and emails are removed automatically.
      </p>
      {result && <p className={`text-xs ${result.ok ? "text-good-ink" : "text-bad-ink"}`}>{result.message}</p>}
    </form>
  );
}
