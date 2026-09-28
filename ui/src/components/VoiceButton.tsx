import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { Transcription } from "../types";
import { Icon } from "./ui";

const MAX_SECONDS = 60;

function pickMime(): { mime: string; ext: string } {
  const options: [string, string][] = [
    ["audio/webm;codecs=opus", "webm"],
    ["audio/webm", "webm"],
    ["audio/mp4", "m4a"],
    ["audio/ogg;codecs=opus", "ogg"],
  ];
  for (const [mime, ext] of options) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(mime)) return { mime, ext };
  }
  return { mime: "", ext: "webm" };
}

/** Hands-free input for gloved technicians: record, then transcribe with Whisper on Groq. */
export default function VoiceButton({
  disabled,
  onTranscript,
  onError,
}: {
  disabled: boolean;
  onTranscript: (t: Transcription) => void;
  onError: (message: string) => void;
}) {
  const [state, setState] = useState<"idle" | "recording" | "transcribing">("idle");
  const [seconds, setSeconds] = useState(0);
  const recorder = useRef<MediaRecorder | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current) window.clearInterval(timer.current);
      recorder.current?.stream.getTracks().forEach((t) => t.stop());
    },
    [],
  );

  async function start() {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      onError("Voice input is not supported in this browser.");
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch {
      onError("Microphone access was blocked. Allow it in the browser to use voice input.");
      return;
    }
    const { mime, ext } = pickMime();
    const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks: Blob[] = [];
    rec.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      if (timer.current) window.clearInterval(timer.current);
      const blob = new Blob(chunks, { type: rec.mimeType || mime || "audio/webm" });
      if (blob.size < 1000) {
        setState("idle");
        onError("That recording was too short. Hold the mic a little longer.");
        return;
      }
      setState("transcribing");
      try {
        onTranscript(await api.transcribe(blob, `voice.${ext}`));
      } catch (err) {
        onError(err instanceof ApiError ? err.message : "Transcription failed.");
      } finally {
        setState("idle");
      }
    };
    recorder.current = rec;
    rec.start();
    setSeconds(0);
    setState("recording");
    timer.current = window.setInterval(() => {
      setSeconds((s) => {
        if (s + 1 >= MAX_SECONDS) rec.state === "recording" && rec.stop();
        return s + 1;
      });
    }, 1000);
  }

  function stop() {
    if (recorder.current?.state === "recording") recorder.current.stop();
  }

  const label =
    state === "recording" ? `Stop recording (${seconds}s)` : state === "transcribing" ? "Transcribing…" : "Speak the fault";
  return (
    <button
      type="button"
      onClick={state === "recording" ? stop : start}
      disabled={disabled || state === "transcribing"}
      aria-label={label}
      title={label}
      className={`flex h-10 shrink-0 items-center gap-1.5 rounded-lg border px-3 text-sm font-medium disabled:opacity-40 ${
        state === "recording"
          ? "border-bad bg-bad-wash text-bad-ink"
          : "border-line text-ink-2 hover:bg-surface-2 hover:text-ink"
      }`}
    >
      {state === "recording" ? (
        <>
          <span className="size-2.5 animate-pulse rounded-full bg-bad" aria-hidden />
          <span className="tabular">{seconds}s</span>
        </>
      ) : state === "transcribing" ? (
        <span className="text-xs">Transcribing…</span>
      ) : (
        <Icon name="mic" className="size-4" />
      )}
    </button>
  );
}
