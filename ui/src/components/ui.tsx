import type { ReactNode } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Tone = "neutral" | "accent" | "good" | "warn" | "bad";

const toneClass: Record<Tone, string> = {
  neutral: "bg-surface-2 text-ink-2 border-line",
  accent: "bg-accent-wash text-accent-ink border-accent-line",
  good: "bg-good-wash text-good-ink border-transparent",
  warn: "bg-warn-wash text-warn-ink border-transparent",
  bad: "bg-bad-wash text-bad-ink border-transparent",
};

export function Badge({ tone = "neutral", children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 whitespace-nowrap ${toneClass[tone]}`}
    >
      {children}
    </span>
  );
}

export function statusTone(status: string): Tone {
  if (["ok", "queued", "hindsight"].includes(status)) return "good";
  if (["fallback", "deferred", "timeout", "skipped", "local_fallback", "local_ledger", "mixed"].includes(status)) return "warn";
  if (status === "error") return "bad";
  return "neutral";
}

export function OutcomeBadge({ held }: { held: boolean | null }) {
  if (held === null) return <Badge>pending</Badge>;
  return held ? (
    <Badge tone="good">
      <Icon name="check" className="size-3" /> held
    </Badge>
  ) : (
    <Badge tone="bad">
      <Icon name="x" className="size-3" /> didn’t hold
    </Badge>
  );
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-answer">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ href, children: c }) => (
            <a href={href} target="_blank" rel="noopener noreferrer" className="text-accent-ink underline">
              {c}
            </a>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

export function JsonBlock({ label, value, open = false }: { label: string; value: unknown; open?: boolean }) {
  return (
    <details open={open} className="group rounded-md border border-line bg-surface-2">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 px-2.5 py-1.5 text-[11px] font-medium text-ink-2 select-none">
        <Icon name="chevron" className="size-3 transition-transform group-open:rotate-90" />
        {label}
      </summary>
      <pre className="max-h-80 overflow-auto border-t border-line px-2.5 py-2 font-mono text-[11px] leading-relaxed text-ink-2 whitespace-pre-wrap break-words">
        {JSON.stringify(value, null, 2)}
      </pre>
    </details>
  );
}

export function Spinner({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-ink-2" role="status" aria-live="polite">
      <span className="flex gap-1" aria-hidden>
        {[0, 1, 2].map((i) => (
          <span key={i} className="pulse-dot size-1.5 rounded-full bg-accent" style={{ animationDelay: `${i * 0.15}s` }} />
        ))}
      </span>
      {label}
    </div>
  );
}

const paths: Record<string, ReactNode> = {
  memory: (
    <>
      <ellipse cx="12" cy="5.5" rx="7" ry="2.5" />
      <path d="M5 5.5v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-6" />
      <path d="M5 11.5v6c0 1.4 3.1 2.5 7 2.5s7-1.1 7-2.5v-6" />
    </>
  ),
  wrench: <path d="M14.7 6.3a4 4 0 0 0-5.4 5.2L3.5 17.3a1.8 1.8 0 0 0 2.5 2.5l5.8-5.8a4 4 0 0 0 5.2-5.4l-2.4 2.4-2.1-.4-.4-2.1z" />,
  book: (
    <>
      <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5z" />
      <path d="M4 20.5A2.5 2.5 0 0 0 6.5 23H20v-5" />
    </>
  ),
  chart: (
    <>
      <path d="M4 20V4" />
      <path d="M4 20h16" />
      <path d="M7 15l4-4 3 3 5-6" />
    </>
  ),
  chevron: <path d="M9 6l6 6-6 6" />,
  x: <path d="M6 6l12 12M18 6L6 18" />,
  check: <path d="M5 12.5l4.5 4.5L19 7.5" />,
  alert: (
    <>
      <path d="M12 3.5l9.5 16.5h-19z" />
      <path d="M12 10v4.5M12 17.5v.01" />
    </>
  ),
  sun: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
    </>
  ),
  moon: <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />,
  send: <path d="M4 12l16-8-6 16-2.5-6.5z" />,
  spark: <path d="M12 3v4M12 17v4M3 12h4M17 12h4M6.3 6.3l2.8 2.8M14.9 14.9l2.8 2.8M6.3 17.7l2.8-2.8M14.9 9.1l2.8-2.8" />,
  users: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M2.5 20a6.5 6.5 0 0 1 13 0" />
      <path d="M16 4.6a3.5 3.5 0 0 1 0 6.8M18.5 20a6.5 6.5 0 0 0-2.8-5.3" />
    </>
  ),
  mic: (
    <>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21M8.5 21h7" />
    </>
  ),
  shield: (
    <>
      <path d="M12 3l7.5 3v5.5c0 4.5-3.2 8.3-7.5 9.5-4.3-1.2-7.5-5-7.5-9.5V6z" />
      <path d="M8.8 12.2l2.2 2.2 4.3-4.6" />
    </>
  ),
  doc: (
    <>
      <path d="M14 3H6.5A1.5 1.5 0 0 0 5 4.5v15A1.5 1.5 0 0 0 6.5 21h11a1.5 1.5 0 0 0 1.5-1.5V8z" />
      <path d="M14 3v5h5M8.5 13h7M8.5 16.5h5" />
    </>
  ),
  download: <path d="M12 4v11M7.5 10.5L12 15l4.5-4.5M5 19.5h14" />,
  refresh: (
    <>
      <path d="M20 11a8 8 0 0 0-14.5-4.5L4 8" />
      <path d="M4 4v4h4" />
      <path d="M4 13a8 8 0 0 0 14.5 4.5L20 16" />
      <path d="M20 20v-4h-4" />
    </>
  ),
};

export function Icon({ name, className = "size-4" }: { name: keyof typeof paths | string; className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      className={className}
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {paths[name]}
    </svg>
  );
}
