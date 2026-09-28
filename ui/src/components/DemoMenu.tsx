import { useEffect, useRef, useState } from "react";
import { api, ApiError, formatDate } from "../api";
import type { DemoStatus } from "../types";
import { Icon } from "./ui";

/** Checkpoint and reset for rehearsals, so the live demo can start from the same memory every time. */
export default function DemoMenu() {
  const [open, setOpen] = useState(false);
  const [status, setStatus] = useState<DemoStatus | null>(null);
  const [label, setLabel] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ ok: boolean; text: string } | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  async function refresh() {
    try {
      setStatus(await api.demoStatus());
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : String(err) });
    }
  }

  useEffect(() => {
    if (!open) return;
    setConfirming(false);
    setMessage(null);
    void refresh();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    const onClick = (e: MouseEvent) => panel.current && !panel.current.contains(e.target as Node) && setOpen(false);
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onClick);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onClick);
    };
  }, [open]);

  async function save() {
    setBusy(true);
    try {
      const cp = await api.saveCheckpoint(label);
      setMessage({ ok: true, text: `Checkpoint saved (${cp.records} app records). Resets will return here.` });
      setLabel("");
      await refresh();
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : String(err) });
    } finally {
      setBusy(false);
    }
  }

  async function reset() {
    setBusy(true);
    try {
      const result = await api.resetDemo();
      setMessage({
        ok: true,
        text: `Reset done: ${result.deleted_documents.length} documents and ${result.deleted_directives.length} directives deleted. Reloading…`,
      });
      window.setTimeout(() => window.location.reload(), 1200); // clears chat threads and cached views
    } catch (err) {
      setMessage({ ok: false, text: err instanceof ApiError ? err.message : String(err) });
      setBusy(false);
    }
  }

  const p = status?.preview;
  const parts = p
    ? ([
        [p.counts.notes, "field note"],
        [p.counts.sessions, "session"],
        [p.counts.outcomes, "outcome"],
        [p.counts.bulletins, "approved bulletin"],
        [p.counts.tickets, "ticket"],
      ] as const).filter(([n]) => n > 0).map(([n, what]) => `${n} ${what}${n === 1 ? "" : "s"}`)
    : [];
  const docs = p ? p.documents.length + p.bulletins.length : 0;

  return (
    <div className="relative" ref={panel}>
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex h-8 items-center gap-1.5 rounded-md border border-line px-2.5 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink"
      >
        <Icon name="refresh" className="size-3.5" />
        Demo
      </button>
      {open && (
        <div
          role="dialog"
          aria-label="Demo checkpoint and reset"
          className="absolute right-0 z-40 mt-2 w-80 space-y-3 rounded-xl border border-line bg-surface p-3 text-sm shadow-panel"
        >
          <div>
            <h3 className="font-semibold">Demo checkpoint</h3>
            <p className="text-xs text-ink-2">
              {status?.checkpoint
                ? `“${status.checkpoint.label || "checkpoint"}” saved ${formatDate(status.checkpoint.created_at)}, ${new Date(status.checkpoint.created_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.`
                : "No checkpoint yet: a reset returns to the seeded data only."}
            </p>
          </div>
          <div className="flex gap-2">
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="Label (optional)"
              maxLength={80}
              className="h-8 min-w-0 flex-1 rounded-md border border-line bg-surface px-2 text-sm"
            />
            <button
              onClick={save}
              disabled={busy}
              className="h-8 rounded-md border border-line px-2.5 text-xs font-medium hover:bg-surface-2 disabled:opacity-50"
            >
              Save checkpoint
            </button>
          </div>
          <div className="border-t border-line pt-3">
            <h3 className="font-semibold">Reset demo</h3>
            {!p ? (
              <p className="text-xs text-ink-3">Checking what changed…</p>
            ) : p.nothing_to_do ? (
              <p className="text-xs text-ink-2">Nothing to reset: memory matches the {p.base.kind === "seed" ? "seeded data" : "checkpoint"}.</p>
            ) : (
              <p className="text-xs text-ink-2">Since the {p.base.kind === "seed" ? "seeded data" : "checkpoint"}: {parts.join(", ")}.</p>
            )}
            {p && !p.nothing_to_do && !confirming && (
              <button
                onClick={() => setConfirming(true)}
                className="mt-2 h-8 rounded-md border border-bad px-2.5 text-xs font-medium text-bad-ink hover:bg-bad-wash"
              >
                Reset demo…
              </button>
            )}
            {confirming && p && (
              <div className="mt-2 space-y-2 rounded-lg bg-bad-wash p-2.5 text-xs text-bad-ink">
                <p>
                  This permanently deletes {docs} document{docs === 1 ? "" : "s"}
                  {p.counts.directives ? ` and ${p.counts.directives} bulletin directive${p.counts.directives === 1 ? "" : "s"}` : ""} from
                  Hindsight and restores the local records. Seeded history and the base safety directives are not touched.
                </p>
                <div className="flex gap-2">
                  <button
                    onClick={reset}
                    disabled={busy}
                    className="h-7 rounded-md bg-bad px-2.5 font-medium text-white hover:brightness-110 disabled:opacity-50"
                  >
                    {busy ? "Deleting…" : "Delete and reset"}
                  </button>
                  <button onClick={() => setConfirming(false)} disabled={busy} className="h-7 rounded-md px-2.5 font-medium text-ink-2 hover:text-ink">
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
          {message && <p className={`text-xs ${message.ok ? "text-good-ink" : "text-bad-ink"}`}>{message.text}</p>}
        </div>
      )}
    </div>
  );
}
