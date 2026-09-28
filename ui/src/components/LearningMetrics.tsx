import { useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { api, formatDate, pct } from "../api";
import type { FleetMetrics, KnowledgeTransfer, WeekMetric } from "../types";
import BenchmarkCard from "./BenchmarkCard";
import { Badge, Icon, Spinner } from "./ui";

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    if (!ref.current) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(ref.current);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

export default function LearningMetrics({ refreshKey }: { refreshKey: number }) {
  const [data, setData] = useState<FleetMetrics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showTable, setShowTable] = useState(false);

  useEffect(() => {
    api
      .metrics()
      .then((m) => {
        setData(m);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [refreshKey]);

  if (error) return <p className="rounded-lg bg-bad-wash px-4 py-3 text-sm text-bad-ink">{error}</p>;
  if (!data) return <Spinner label="Loading fleet metrics…" />;

  const s = data.summary;
  const first = data.weeks[0];
  const last = data.weeks[data.weeks.length - 1];

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold">Fleet learning curve</h2>
        <p className="max-w-3xl text-sm text-ink-2">
          {s.total_jobs} work orders across {data.weeks.length} weekly service cycles ({formatDate(first.start)} to{" "}
          {formatDate(last.end)}). As more repairs are retained, technicians fix more faults on the first visit because they
          start from what colleagues already verified.
        </p>
      </div>

      {/* Figures: one hero, then supporting tiles */}
      <div className="grid gap-3 sm:grid-cols-3 xl:grid-cols-[1.4fr_1fr_1fr_1fr]">
        <div className="rounded-xl border border-line bg-surface p-4 shadow-panel sm:col-span-3 xl:col-span-1">
          <div className="text-sm text-ink-2">First-time-fix rate, {last.label.toLowerCase()}</div>
          <div className="mt-1 flex items-baseline gap-3">
            <span className="text-5xl font-semibold tracking-tight">{pct(s.ftf_end)}</span>
            <span className="text-sm font-medium text-good-ink">▲ {s.ftf_delta_pts} pts vs {first.label.toLowerCase()}</span>
          </div>
          <div className="mt-1 text-xs text-ink-3">
            {first.label}: {pct(s.ftf_start)} · {last.first_time_fixes} of {last.jobs} jobs fixed on the first visit
          </div>
        </div>
        <Tile label="Callbacks avoided last week" value={s.callbacks_avoided_last_week.toFixed(0)} note={`vs the ${first.label.toLowerCase()} callback rate`} />
        <Tile label="Field discoveries retained" value={String(s.field_discoveries)} note={`reused ${s.peer_reuses}× by other technicians`} />
        <Tile
          label="Memory records"
          value={String(s.memory_records_seeded + s.memory_records_live)}
          note={s.memory_records_live ? `${s.memory_records_seeded} seeded + ${s.memory_records_live} from live sessions` : "seeded service history"}
        />
      </div>

      {/* Charts: two single-measure charts on a shared x, never a dual axis */}
      <section className="rounded-xl border border-line bg-surface p-4 shadow-panel">
        <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold">First-time-fix rate by service week</h3>
            <p className="text-xs text-ink-2">Share of work orders whose first visit’s fix held with no callback</p>
          </div>
          <button
            onClick={() => setShowTable((v) => !v)}
            className="rounded-md border border-line px-2.5 py-1 text-xs text-ink-2 hover:bg-surface-2"
            aria-pressed={showTable}
          >
            {showTable ? "Show chart" : "Show table"}
          </button>
        </div>
        {showTable ? <WeekTable weeks={data.weeks} /> : <WeeklyCharts weeks={data.weeks} />}
      </section>

      <BenchmarkCard />

      <div className="grid gap-5 xl:grid-cols-[1.6fr_1fr]">
        <PeerLedger transfers={data.knowledge_transfers} period={[first.start, last.end]} />
        <section className="rounded-xl border border-line bg-surface p-4 shadow-panel">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Icon name="users" className="size-4 text-ink-2" /> First-time-fix by technician
          </h3>
          <p className="mb-3 text-xs text-ink-2">First visits in weeks 1–2 vs weeks 3–4</p>
          <table className="tabular w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs text-ink-2">
                <th className="py-1.5 font-medium">Technician</th>
                <th className="py-1.5 text-right font-medium">Wk 1–2</th>
                <th className="py-1.5 text-right font-medium">Wk 3–4</th>
                <th className="py-1.5 text-right font-medium">Change</th>
              </tr>
            </thead>
            <tbody>
              {data.technicians.map((t) => {
                const change = Math.round((t.ftf_weeks_3_4 - t.ftf_weeks_1_2) * 100);
                return (
                  <tr key={t.technician} className="border-b border-line last:border-0">
                    <td className="py-2">
                      <div className="font-medium">{t.technician}</div>
                      <div className="text-xs text-ink-3">
                        {t.role} · {t.jobs} jobs
                      </div>
                    </td>
                    <td className="py-2 text-right">{pct(t.ftf_weeks_1_2)}</td>
                    <td className="py-2 text-right">{pct(t.ftf_weeks_3_4)}</td>
                    <td className={`py-2 text-right font-medium ${change > 0 ? "text-good-ink" : change < 0 ? "text-bad-ink" : "text-ink-2"}`}>
                      {change > 0 ? "▲" : change < 0 ? "▼" : ""} {Math.abs(change)} pts
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <p className="mt-3 text-xs text-ink-3">
            Live sessions: {data.live.tickets_open} open ticket{data.live.tickets_open === 1 ? "" : "s"},{" "}
            {data.live.tickets_confirmed} confirmed ({data.live.tickets_held} held).
          </p>
        </section>
      </div>
      <p className="text-xs text-ink-3">{data.definition}</p>
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="rounded-xl border border-line bg-surface p-4 shadow-panel">
      <div className="text-sm text-ink-2">{label}</div>
      <div className="mt-1 text-3xl font-semibold tracking-tight">{value}</div>
      <div className="mt-1 text-xs text-ink-3">{note}</div>
    </div>
  );
}

// ------------------------------------------------------------------ charts
const M = { left: 44, right: 56, top: 18 };
const INSET = 32; // keeps the first/last category off the axis labels

function WeeklyCharts({ weeks }: { weeks: WeekMetric[] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const plotW = Math.max(0, width - M.left - M.right - 2 * INSET);
  const x = (i: number) => M.left + INSET + (weeks.length === 1 ? plotW / 2 : (i / (weeks.length - 1)) * plotW);

  const pick = (e: PointerEvent<SVGRectElement>) => {
    const bounds = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - bounds.left + M.left;
    let best = 0;
    weeks.forEach((_, i) => {
      if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i;
    });
    setActive(best);
  };
  const onKey = (e: KeyboardEvent) => {
    if (e.key === "ArrowRight") setActive((a) => Math.min(weeks.length - 1, (a ?? -1) + 1));
    if (e.key === "ArrowLeft") setActive((a) => Math.max(0, (a ?? weeks.length) - 1));
    if (e.key === "Escape") setActive(null);
  };

  const w = active !== null ? weeks[active] : null;
  return (
    <div
      ref={ref}
      className="relative"
      tabIndex={0}
      onKeyDown={onKey}
      onBlur={() => setActive(null)}
      aria-label="First-time-fix rate and memory density by week. Use arrow keys to read values."
    >
      {width > 0 && (
        <>
          <FtfLine weeks={weeks} width={width} x={x} active={active} onPick={pick} onLeave={() => setActive(null)} />
          <div className="mt-4 mb-1 text-xs font-semibold text-ink">Memory density</div>
          <div className="mb-1 text-xs text-ink-2">Cumulative repair records retained by the end of each week</div>
          <DensityColumns weeks={weeks} width={width} x={x} active={active} onPick={setActive} />
        </>
      )}
      {w && active !== null && (
        <div
          className="pointer-events-none absolute top-2 z-10 w-52 rounded-lg border border-line bg-surface p-2.5 text-xs shadow-panel"
          style={{ left: Math.min(Math.max(0, x(active) + 12), Math.max(0, width - 216)) }}
          role="status"
        >
          <div className="mb-1 text-ink-2">
            {w.label} · {formatDate(w.start)}–{formatDate(w.end)}
          </div>
          <Row k="First-time-fix" v={pct(w.ftf_rate)} strong />
          <Row k="Fixed first visit" v={`${w.first_time_fixes} of ${w.jobs}`} />
          <Row k="Callbacks" v={String(w.callbacks)} />
          <Row k="Peer-memory first visits" v={String(w.peer_assisted_jobs)} />
          <Row k="Memory records" v={String(w.memory_records)} />
        </div>
      )}
    </div>
  );
}

function Row({ k, v, strong }: { k: string; v: string; strong?: boolean }) {
  return (
    <div className="tabular flex justify-between gap-3 py-0.5">
      <span className="text-ink-2">{k}</span>
      <span className={strong ? "font-semibold text-ink" : "text-ink"}>{v}</span>
    </div>
  );
}

function FtfLine({
  weeks,
  width,
  x,
  active,
  onPick,
  onLeave,
}: {
  weeks: WeekMetric[];
  width: number;
  x: (i: number) => number;
  active: number | null;
  onPick: (e: PointerEvent<SVGRectElement>) => void;
  onLeave: () => void;
}) {
  const H = 220;
  const bottom = 30;
  const [lo, hi] = [0.4, 1.0];
  const y = (v: number) => M.top + (1 - (v - lo) / (hi - lo)) * (H - M.top - bottom);
  const ticks = [0.4, 0.6, 0.8, 1.0];
  const pts = weeks.map((w, i) => [x(i), y(w.ftf_rate)] as const);
  const line = pts.map(([px, py], i) => `${i ? "L" : "M"}${px},${py}`).join(" ");
  const area = `${line} L${pts[pts.length - 1][0]},${y(lo)} L${pts[0][0]},${y(lo)} Z`;
  const firstW = weeks[0];
  const lastW = weeks[weeks.length - 1];

  return (
    <svg width={width} height={H} role="img" aria-label={`First-time-fix rose from ${pct(firstW.ftf_rate)} to ${pct(lastW.ftf_rate)}`}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={M.left} x2={width - M.right} y1={y(t)} y2={y(t)} stroke="var(--grid)" strokeWidth={1} />
          <text x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="tabular" fontSize={11} fill="var(--ink-3)">
            {Math.round(t * 100)}%
          </text>
        </g>
      ))}
      {weeks.map((w, i) => (
        <text key={w.week} x={x(i)} y={H - 10} textAnchor="middle" fontSize={11} fill="var(--ink-3)">
          {w.label}
        </text>
      ))}
      <path d={area} fill="var(--series-1)" opacity={0.1} />
      <path d={line} fill="none" stroke="var(--series-1)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {active !== null && (
        <line x1={x(active)} x2={x(active)} y1={M.top - 6} y2={y(lo)} stroke="var(--axis)" strokeWidth={1} />
      )}
      {pts.map(([px, py], i) => (
        <circle key={i} cx={px} cy={py} r={active === i ? 5.5 : 4} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2} />
      ))}
      {/* Selective direct labels: where it started and where it ended */}
      <text x={pts[0][0]} y={pts[0][1] + 20} textAnchor="middle" fontSize={12} fontWeight={600} fill="var(--ink)">
        {pct(firstW.ftf_rate)}
      </text>
      <text x={pts[pts.length - 1][0] + 10} y={pts[pts.length - 1][1]} dy="0.32em" fontSize={12} fontWeight={600} fill="var(--ink)">
        {pct(lastW.ftf_rate)}
      </text>
      <rect
        x={M.left}
        y={0}
        width={Math.max(0, width - M.left - M.right)}
        height={H - bottom}
        fill="transparent"
        onPointerMove={onPick}
        onPointerLeave={onLeave}
      />
    </svg>
  );
}

function DensityColumns({
  weeks,
  width,
  x,
  active,
  onPick,
}: {
  weeks: WeekMetric[];
  width: number;
  x: (i: number) => number;
  active: number | null;
  onPick: (i: number | null) => void;
}) {
  const H = 130;
  const bottom = 22;
  const max = Math.max(...weeks.map((w) => w.memory_records));
  const niceMax = Math.ceil(max / 25) * 25;
  const y = (v: number) => 8 + (1 - v / niceMax) * (H - 8 - bottom);
  const barW = 24;
  const ticks = [0, niceMax / 2, niceMax];
  return (
    <svg width={width} height={H} role="img" aria-label={`Memory records grew from ${weeks[0].memory_records} to ${max}`}>
      {ticks.map((t) => (
        <g key={t}>
          <line x1={M.left} x2={width - M.right} y1={y(t)} y2={y(t)} stroke={t === 0 ? "var(--axis)" : "var(--grid)"} strokeWidth={1} />
          <text x={M.left - 8} y={y(t)} dy="0.32em" textAnchor="end" className="tabular" fontSize={11} fill="var(--ink-3)">
            {t}
          </text>
        </g>
      ))}
      {weeks.map((w, i) => {
        const top = y(w.memory_records);
        const h = y(0) - top;
        const left = x(i) - barW / 2;
        const r = Math.min(4, h);
        // 4px rounded data-end, square at the baseline
        const d = `M${left},${y(0)} V${top + r} Q${left},${top} ${left + r},${top} H${left + barW - r} Q${left + barW},${top} ${left + barW},${top + r} V${y(0)} Z`;
        return (
          <g key={w.week} onPointerEnter={() => onPick(i)} onPointerLeave={() => onPick(null)}>
            <rect x={x(i) - 24} y={0} width={48} height={H - bottom} fill="transparent" />
            <path d={d} fill="var(--series-1)" opacity={active === null || active === i ? 1 : 0.55} />
            {i === weeks.length - 1 && (
              <text x={x(i)} y={top - 6} textAnchor="middle" fontSize={12} fontWeight={600} fill="var(--ink)">
                {w.memory_records}
              </text>
            )}
            <text x={x(i)} y={H - 6} textAnchor="middle" fontSize={11} fill="var(--ink-3)">
              {w.label}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function WeekTable({ weeks }: { weeks: WeekMetric[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="tabular w-full min-w-[40rem] text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs text-ink-2">
            {["Week", "Dates", "Jobs", "Fixed first visit", "First-time-fix", "Callbacks", "Peer-memory first visits", "Memory records"].map((h) => (
              <th key={h} className="py-1.5 pr-3 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {weeks.map((w) => (
            <tr key={w.week} className="border-b border-line last:border-0">
              <td className="py-2 pr-3 font-medium">{w.label}</td>
              <td className="py-2 pr-3 text-ink-2">
                {formatDate(w.start)}–{formatDate(w.end)}
              </td>
              <td className="py-2 pr-3">{w.jobs}</td>
              <td className="py-2 pr-3">{w.first_time_fixes}</td>
              <td className="py-2 pr-3 font-semibold">{pct(w.ftf_rate, 1)}</td>
              <td className="py-2 pr-3">{w.callbacks}</td>
              <td className="py-2 pr-3">{w.peer_assisted_jobs}</td>
              <td className="py-2 pr-3">{w.memory_records}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ ledger
function PeerLedger({ transfers, period }: { transfers: KnowledgeTransfer[]; period: [string, string] }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const t0 = new Date(`${period[0]}T00:00:00Z`).getTime();
  const t1 = new Date(`${period[1]}T23:59:59Z`).getTime();
  const trackW = Math.max(0, width - 16);
  const px = (iso: string) => 8 + ((new Date(`${iso}T12:00:00Z`).getTime() - t0) / (t1 - t0)) * trackW;

  return (
    <section className="rounded-xl border border-line bg-surface p-4 shadow-panel">
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        <Icon name="memory" className="size-4 text-accent-ink" /> Peer learning ledger
      </h3>
      <p className="mb-3 text-xs text-ink-2">
        Field findings one technician retained, and every later job where a colleague applied them from memory
      </p>
      <ul className="divide-y divide-line">
        {transfers.map((t) => {
          const peers = t.reuses.filter((r) => r.peer);
          return (
            <li key={`${t.knowledge_key}-${t.discovered_on}`} className="py-3 first:pt-0 last:pb-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <Badge tone="accent">{t.error_code}</Badge>
                <span className="text-xs text-ink-2">{t.model_name}</span>
                <span className="tabular ml-auto text-xs text-ink-2">
                  reused {t.reuse_count}× · held {t.reuse_held}/{t.reuse_count}
                </span>
              </div>
              <p className="mt-1 text-sm">{t.finding}</p>
              <p className="mt-0.5 text-xs text-ink-2">
                Found by <strong className="font-medium text-ink">{t.discovered_by}</strong> ({t.discovered_role}) on{" "}
                {formatDate(t.discovered_on)} at {t.discovered_unit}
                {t.beneficiaries.length > 0 && (
                  <>
                    {" "}
                    → helped <strong className="font-medium text-ink">{t.beneficiaries.join(", ")}</strong> on {peers.length} later job
                    {peers.length === 1 ? "" : "s"}
                  </>
                )}
              </p>
              <div ref={t === transfers[0] ? ref : undefined} className="mt-2">
                {width > 0 && (
                  <svg width={width} height={22} aria-hidden>
                    <line x1={8} x2={width - 8} y1={11} y2={11} stroke="var(--grid)" strokeWidth={1} />
                    {t.reuses.map((r) => (
                      <circle
                        key={r.work_order}
                        cx={px(r.date)}
                        cy={11}
                        r={4}
                        fill={r.held ? "var(--series-muted)" : "var(--surface)"}
                        stroke={r.held ? "var(--surface)" : "var(--ink-3)"}
                        strokeWidth={r.held ? 2 : 1.5}
                      >
                        <title>{`${r.technician} · ${formatDate(r.date)} · ${r.unit_id} · ${r.held ? "held" : "not the cause"}`}</title>
                      </circle>
                    ))}
                    <circle cx={px(t.discovered_on)} cy={11} r={5.5} fill="var(--series-1)" stroke="var(--surface)" strokeWidth={2}>
                      <title>{`Discovered by ${t.discovered_by} · ${formatDate(t.discovered_on)}`}</title>
                    </circle>
                  </svg>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      <div className="mt-3 flex flex-wrap items-center gap-4 text-xs text-ink-2">
        <span className="flex items-center gap-1.5">
          <svg width={12} height={12} aria-hidden>
            <circle cx={6} cy={6} r={5} fill="var(--series-1)" />
          </svg>
          discovery
        </span>
        <span className="flex items-center gap-1.5">
          <svg width={12} height={12} aria-hidden>
            <circle cx={6} cy={6} r={4} fill="var(--series-muted)" />
          </svg>
          reused, fix held
        </span>
        <span className="flex items-center gap-1.5">
          <svg width={12} height={12} aria-hidden>
            <circle cx={6} cy={6} r={4} fill="none" stroke="var(--ink-3)" strokeWidth={1.5} />
          </svg>
          checked, not the cause
        </span>
        <span className="tabular ml-auto text-ink-3">
          {formatDate(period[0])} → {formatDate(period[1])}
        </span>
      </div>
    </section>
  );
}
