import { useEffect, useState } from "react";
import { api, ApiError, formatDate } from "../api";
import type { Bulletin, BulletinCandidate } from "../types";
import { Badge, Icon, Spinner } from "./ui";

/** Service-manager view: turn field evidence into Technical Service Bulletins. */
export default function BulletinsPanel({ onPublished }: { onPublished: () => void }) {
  const [candidates, setCandidates] = useState<BulletinCandidate[] | null>(null);
  const [bulletins, setBulletins] = useState<Bulletin[]>([]);
  const [selected, setSelected] = useState<Bulletin | null>(null);
  const [drafting, setDrafting] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function load(selectId?: string) {
    try {
      const data = await api.bulletins();
      setCandidates(data.candidates);
      setBulletins(data.bulletins);
      if (selectId) setSelected(data.bulletins.find((b) => b.id === selectId) ?? null);
      else setSelected((cur) => cur ?? data.bulletins[0] ?? null);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function draft(c: BulletinCandidate) {
    const key = `${c.model_key}/${c.error_code}`;
    setDrafting(key);
    setError(null);
    try {
      const b = await api.draftBulletin(c.model_key, c.error_code);
      await load(b.id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setDrafting(null);
    }
  }

  if (error && !candidates) return <p className="rounded-lg bg-bad-wash px-4 py-3 text-sm text-bad-ink">{error}</p>;
  if (!candidates) return <Spinner label="Loading field evidence…" />;

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Field bulletins</h2>
        <p className="max-w-3xl text-sm text-ink-2">
          When technicians keep proving the OEM manual wrong, turn that tribal knowledge into an official Technical Service
          Bulletin. Hindsight drafts it under your safety directives; the evidence numbers come straight from the repair
          ledger. Approving publishes it to memory and installs a guardrail scoped to that fault.
        </p>
      </div>
      {error && <p className="rounded-lg bg-bad-wash px-3 py-2 text-sm text-bad-ink">{error}</p>}
      <div className="grid gap-4 lg:grid-cols-[minmax(0,23rem)_minmax(0,1fr)]">
        <CandidateList
          candidates={candidates}
          bulletins={bulletins}
          selectedId={selected?.id}
          drafting={drafting}
          onDraft={draft}
          onSelect={setSelected}
        />
        <section aria-label="Bulletin document" className="min-w-0">
          {drafting ? (
            <div className="rounded-xl border border-line bg-surface p-6 shadow-panel">
              <Spinner label="Hindsight is drafting the bulletin from retained repairs under your safety directives (about 15 s)…" />
            </div>
          ) : selected ? (
            <BulletinDocument
              bulletin={selected}
              onApproved={async (b) => {
                await load(b.id);
                onPublished();
              }}
            />
          ) : (
            <p className="rounded-xl border border-dashed border-line px-4 py-10 text-center text-sm text-ink-2">
              Pick a fault on the left and draft its bulletin.
            </p>
          )}
        </section>
      </div>
    </div>
  );
}

function CandidateList({
  candidates,
  bulletins,
  selectedId,
  drafting,
  onDraft,
  onSelect,
}: {
  candidates: BulletinCandidate[];
  bulletins: Bulletin[];
  selectedId?: string;
  drafting: string | null;
  onDraft: (c: BulletinCandidate) => void;
  onSelect: (b: Bulletin) => void;
}) {
  return (
    <section className="space-y-2" aria-label="Bulletin candidates">
      <h3 className="text-xs font-semibold tracking-wide text-ink-2 uppercase">Where the field beats the manual</h3>
      {candidates.length === 0 && (
        <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-ink-2">
          No fault has enough field evidence yet.
        </p>
      )}
      {candidates.map((c) => {
        const best = c.evidence.field_fixes[0];
        const key = `${c.model_key}/${c.error_code}`;
        const existing = c.bulletin ? bulletins.find((b) => b.id === c.bulletin!.id) : undefined;
        return (
          <article
            key={key}
            className={`rounded-xl border bg-surface p-3 shadow-panel ${
              existing && selectedId === existing.id ? "border-accent-line" : "border-line"
            }`}
          >
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="accent">{c.error_code}</Badge>
              <span className="text-xs text-ink-2">{c.model_name}</span>
              {c.bulletin && (
                <Badge tone={c.bulletin.status === "approved" ? "good" : "neutral"}>
                  {c.bulletin.id} · {c.bulletin.status}
                </Badge>
              )}
            </div>
            <p className="mt-1 text-sm font-medium">{c.error_title}</p>
            <p className="mt-1 text-xs text-ink-2">
              Field fix held <strong className="tabular text-ink">{best.held}/{best.attempts}</strong>, first by{" "}
              {best.first_confirmed_by} on {formatDate(best.first_confirmed_on)} · OEM step held{" "}
              <strong className="tabular text-ink">
                {c.evidence.oem_step.held}/{c.evidence.oem_step.attempts}
              </strong>
              {c.evidence.ruled_out.length > 0 && ` · ruled out ${c.evidence.ruled_out.length}×`}
            </p>
            <div className="mt-2 flex gap-2">
              {existing ? (
                <button
                  onClick={() => onSelect(existing)}
                  className="rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-surface-2"
                >
                  View bulletin
                </button>
              ) : (
                <button
                  onClick={() => onDraft(c)}
                  disabled={drafting !== null}
                  className="flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-white hover:brightness-110 disabled:opacity-50"
                >
                  <Icon name="doc" className="size-3.5" />
                  {drafting === key ? "Drafting…" : "Draft bulletin"}
                </button>
              )}
            </div>
          </article>
        );
      })}
      {bulletins.length > 0 && (
        <>
          <h3 className="pt-3 text-xs font-semibold tracking-wide text-ink-2 uppercase">All bulletins</h3>
          <ul className="divide-y divide-line rounded-xl border border-line bg-surface">
            {bulletins.map((b) => (
              <li key={b.id}>
                <button
                  onClick={() => onSelect(b)}
                  className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-surface-2 ${
                    selectedId === b.id ? "bg-accent-wash" : ""
                  }`}
                >
                  <span className="font-mono text-xs">{b.id}</span>
                  <span className="truncate text-ink-2">{b.error_code}</span>
                  <Badge tone={b.status === "approved" ? "good" : "neutral"}>{b.status}</Badge>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function BulletinDocument({ bulletin: b, onApproved }: { bulletin: Bulletin; onApproved: (b: Bulletin) => void }) {
  const c = b.content;
  const ev = b.evidence;
  return (
    <article className="rounded-xl border border-line bg-surface shadow-panel">
      <header className="flex flex-wrap items-center gap-2 border-b border-line px-5 py-3">
        <span className="font-mono text-sm font-semibold">{b.id}</span>
        <Badge tone={b.status === "approved" ? "good" : "neutral"}>{b.status}</Badge>
        <Badge tone={c.confidence === "high" ? "good" : c.confidence === "medium" ? "neutral" : "warn"}>
          {c.confidence} confidence
        </Badge>
        <Badge tone={b.generator === "hindsight_reflect" ? "accent" : "neutral"}>
          {b.generator === "hindsight_reflect" ? "drafted by Hindsight reflect" : "template draft"}
        </Badge>
        <button
          onClick={() => void api.downloadBulletin(b.id)}
          className="ml-auto flex items-center gap-1.5 rounded-md border border-line px-2.5 py-1 text-xs font-medium hover:bg-surface-2"
        >
          <Icon name="download" className="size-3.5" /> Download .md
        </button>
      </header>
      <div className="space-y-4 px-5 py-4 text-sm">
        <div>
          <h3 className="text-base font-semibold">{c.title}</h3>
          <p className="text-xs text-ink-2">
            Applies to {b.model_name}, error {b.error_code} ({b.error_title}) · evidence{" "}
            {ev.period ? `${formatDate(ev.period[0])} to ${formatDate(ev.period[1])}` : "n/a"} · {ev.records} recorded outcomes
          </p>
        </div>
        <Section title="Symptom">{c.symptom}</Section>
        <Section title="Root cause">{c.root_cause}</Section>
        <Section title="Recommended procedure">
          <ol className="list-decimal space-y-1 pl-5">
            {c.recommended_procedure.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ol>
        </Section>
        <Section title="When to use the OEM procedure">{c.when_to_use_oem_procedure}</Section>
        {c.site_conditions && <Section title="Site conditions">{c.site_conditions}</Section>}
        <EvidenceTable bulletin={b} />
        <p className="text-xs text-ink-3">
          {b.generator === "hindsight_reflect"
            ? `Drafted by Hindsight structured reflect from ${b.memories_consulted} memories under directives: ${b.directives_applied.join(", ") || "none"}.`
            : `Template draft (Hindsight unavailable${b.generator_error ? `: ${b.generator_error}` : ""}).`}
        </p>
      </div>
      <ApprovalFooter bulletin={b} onApproved={onApproved} />
    </article>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h4 className="mb-0.5 text-xs font-semibold tracking-wide text-ink-2 uppercase">{title}</h4>
      <div className="text-ink">{children}</div>
    </div>
  );
}

function EvidenceTable({ bulletin: b }: { bulletin: Bulletin }) {
  const ev = b.evidence;
  return (
    <div>
      <h4 className="mb-1.5 text-xs font-semibold tracking-wide text-ink-2 uppercase">Field evidence (from the repair ledger)</h4>
      <div className="overflow-x-auto">
        <table className="tabular w-full min-w-[32rem] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs text-ink-2">
              <th className="py-1.5 pr-3 font-medium">Fix</th>
              <th className="py-1.5 pr-3 font-medium">Held</th>
              <th className="py-1.5 font-medium">First confirmed</th>
            </tr>
          </thead>
          <tbody>
            <tr className="border-b border-line">
              <td className="py-2 pr-3">
                <Badge>OEM</Badge> {ev.oem_step.action}
              </td>
              <td className="py-2 pr-3 font-semibold">
                {ev.oem_step.held}/{ev.oem_step.attempts}
              </td>
              <td className="py-2 text-ink-2">manual</td>
            </tr>
            {ev.field_fixes.map((f) => (
              <tr key={f.action} className="border-b border-line last:border-0">
                <td className="py-2 pr-3">
                  <Badge tone="accent">field</Badge> {f.action}
                </td>
                <td className="py-2 pr-3 font-semibold">
                  {f.held}/{f.attempts}
                </td>
                <td className="py-2 text-ink-2">
                  {f.first_confirmed_by}, {formatDate(f.first_confirmed_on)} ({f.first_unit})
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {ev.ruled_out.length > 0 && (
        <p className="mt-1.5 text-xs text-ink-2">
          Field pattern checked and ruled out on {ev.ruled_out.map((r) => `${r.unit_id} (${formatDate(r.date)})`).join(", ")}.
        </p>
      )}
    </div>
  );
}

function ApprovalFooter({ bulletin: b, onApproved }: { bulletin: Bulletin; onApproved: (b: Bulletin) => void }) {
  const [approver, setApprover] = useState("Service Manager");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function approve() {
    setBusy(true);
    setError(null);
    try {
      onApproved(await api.approveBulletin(b.id, approver.trim() || "Service Manager"));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <footer className="border-t border-line px-5 py-3">
      {b.status === "approved" ? (
        <div className="space-y-1 text-xs text-ink-2">
          <p className="flex items-center gap-1.5 font-medium text-good-ink">
            <Icon name="check" className="size-3.5" /> Approved by {b.approved_by} on {formatDate(b.approved_at)}
          </p>
          {b.publish && (
            <p>
              Memory: {b.publish.memory} · Guardrail directive: {b.publish.directive}
            </p>
          )}
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-xs text-ink-2">
            Approver
            <input
              value={approver}
              onChange={(e) => setApprover(e.target.value)}
              maxLength={60}
              className="h-8 rounded-md border border-line bg-surface px-2 text-sm text-ink"
            />
          </label>
          <button
            onClick={approve}
            disabled={busy}
            className="flex h-8 items-center gap-1.5 rounded-md bg-accent px-3 text-sm font-medium text-white hover:brightness-110 disabled:opacity-50"
          >
            <Icon name="shield" className="size-3.5" />
            {busy ? "Publishing…" : "Approve & publish to memory"}
          </button>
          {error && <span className="text-xs text-bad-ink">{error}</span>}
        </div>
      )}
    </footer>
  );
}
