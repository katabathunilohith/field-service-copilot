import { useEffect, useState } from "react";
import { api, formatDate } from "../api";
import type { EvalReport, Rate } from "../types";
import { Badge, Icon } from "./ui";

function Score({ rate, tone }: { rate: Rate; tone: "accent" | "neutral" }) {
  const share = rate.total ? rate.hits / rate.total : 0;
  return (
    <div className="flex items-center justify-end gap-2">
      <span className="tabular font-semibold">
        {rate.hits}/{rate.total}
      </span>
      <span
        className={`h-1.5 w-16 overflow-hidden rounded-full ${tone === "accent" ? "bg-accent-wash" : "bg-surface-3"}`}
        role="img"
        aria-label={`${rate.hits} of ${rate.total}`}
      >
        <span className={`block h-full rounded-full ${tone === "accent" ? "bg-accent" : "bg-ink-3"}`} style={{ width: `${share * 100}%` }} />
      </span>
    </div>
  );
}

/** Results of `python eval/run_eval.py`: the same questions, with and without memory. */
export default function BenchmarkCard() {
  const [report, setReport] = useState<EvalReport | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    api.evalLatest().then(setReport).catch(() => setReport({ available: false }));
  }, []);

  if (!report) return null;
  if (!report.available || !report.summary || !report.rows)
    return (
      <section className="rounded-xl border border-dashed border-line p-4 text-sm text-ink-2">
        <h3 className="mb-1 font-semibold text-ink">Benchmark</h3>
        Run <code className="font-mono text-xs">python eval/run_eval.py</code> to score the Baseline agent against the
        Copilot on the same field scenarios.
      </section>
    );

  const s = report.summary;
  const rows: [string, Rate | null, Rate][] = [
    ["Right root cause", s.baseline.root_cause_ok, s.copilot.root_cause_ok],
    ["Right fix first, where the field beats the manual", s.tricky_first_fix.baseline, s.tricky_first_fix.copilot],
    ["Right fix first, where the manual is correct", s.control_first_fix.baseline, s.control_first_fix.copilot],
    ["Safety step included", s.baseline.safety_ok, s.copilot.safety_ok],
    ["Every citation verified against the ledger", null, s.copilot.citations_ok],
  ];

  return (
    <section className="rounded-xl border border-line bg-surface p-4 shadow-panel" aria-label="Benchmark">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Icon name="check" className="size-4 text-ink-2" /> Benchmark: same questions, with and without memory
          </h3>
          <p className="text-xs text-ink-2">
            {s.scenarios} field scenarios · {report.model} · memory: {report.memory} · run {formatDate(report.generated_at)} ·
            read-only (nothing retained)
          </p>
        </div>
        <Badge tone="good">
          ${s.avoided_parts_usd.toLocaleString()} parts and ~{s.avoided_labor_h} h avoided
        </Badge>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs text-ink-2">
            <th className="py-1.5 font-medium">Check</th>
            <th className="py-1.5 text-right font-medium">Baseline (manual only)</th>
            <th className="py-1.5 text-right font-medium">Hindsight Copilot</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, base, cop]) => (
            <tr key={label} className="border-b border-line last:border-0">
              <td className="py-2 pr-3">{label}</td>
              <td className="py-2">{base ? <Score rate={base} tone="neutral" /> : <span className="block text-right text-ink-3">n/a</span>}</td>
              <td className="py-2">
                <Score rate={cop} tone="accent" />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button onClick={() => setOpen((v) => !v)} className="mt-3 text-xs font-medium text-accent-ink hover:underline" aria-expanded={open}>
        {open ? "Hide scenarios" : "Show every scenario"}
      </button>
      {open && (
        <div className="mt-2 overflow-x-auto">
          <table className="w-full min-w-[44rem] text-xs">
            <thead>
              <tr className="border-b border-line text-left text-ink-2">
                <th className="py-1.5 pr-2 font-medium">Scenario</th>
                <th className="py-1.5 pr-2 font-medium">Baseline leads with</th>
                <th className="py-1.5 font-medium">Copilot leads with</th>
              </tr>
            </thead>
            <tbody>
              {report.rows.map((r) => (
                <tr key={r.id} className="border-b border-line align-top last:border-0">
                  <td className="py-2 pr-2">
                    <div className="font-medium">
                      {r.error_code} · {r.unit}
                    </div>
                    <div className="text-ink-3">{r.kind === "manual" ? "control: manual is right" : "field pattern"}</div>
                  </td>
                  <td className="py-2 pr-2">
                    <Mark ok={r.baseline.first_fix_ok} /> {r.baseline.first_fix.replace(/\*\*/g, "")}
                  </td>
                  <td className="py-2">
                    <Mark ok={r.copilot.first_fix_ok} /> {r.copilot.first_fix.replace(/\*\*/g, "")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Mark({ ok }: { ok: boolean }) {
  return ok ? (
    <Icon name="check" className="inline size-3.5 text-good-ink" />
  ) : (
    <Icon name="x" className="inline size-3.5 text-bad-ink" />
  );
}
