"use client";

import { createRoot } from "react-dom/client";
import { useEffect, useRef, useState } from "react";

type Tone = "accent" | "danger" | "warn";

const toneBtnClass = (tone: Tone) =>
  tone === "danger"
    ? "border-danger/60 bg-danger/10 hover:bg-danger/20 text-danger"
    : tone === "warn"
      ? "border-warn/60 bg-warn/10 hover:bg-warn/20 text-warn"
      : "border-accent/60 bg-accent/10 hover:bg-accent/20 text-accent";

const toneAccent = (tone: Tone) =>
  tone === "danger"
    ? "text-danger"
    : tone === "warn"
      ? "text-warn"
      : "text-accent";

interface ConfirmOpts {
  title?: string;
  message: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: Tone;
}

interface PromptOpts {
  title?: string;
  message: string;
  placeholder?: string;
  defaultValue?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: Tone;
}

function mount<T>(render: (close: (v: T) => void) => React.ReactNode): Promise<T> {
  return new Promise((resolve) => {
    const host = document.createElement("div");
    document.body.appendChild(host);
    const root = createRoot(host);
    const close = (v: T) => {
      root.unmount();
      host.remove();
      resolve(v);
    };
    root.render(render(close));
  });
}

export function confirmDialog(opts: ConfirmOpts): Promise<boolean> {
  return mount<boolean>((close) => (
    <ConfirmModal opts={opts} onClose={close} />
  ));
}

export function promptDialog(opts: PromptOpts): Promise<string | null> {
  return mount<string | null>((close) => (
    <PromptModal opts={opts} onClose={close} />
  ));
}

function Shell({
  tone,
  title,
  onCancel,
  children,
}: {
  tone: Tone;
  title: string;
  onCancel: () => void;
  children: React.ReactNode;
}) {
  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center p-4 bg-black/70 backdrop-blur-sm"
      onClick={onCancel}
    >
      <div
        className="frame max-w-md w-full p-5 space-y-4"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="flex items-center justify-between">
          <div
            className={`text-[11px] uppercase tracking-widest font-semibold ${toneAccent(
              tone,
            )}`}
          >
            <span className="mr-2">▸</span>
            {title}
          </div>
          <button
            onClick={onCancel}
            aria-label="close"
            className="text-text-faint hover:text-text text-sm"
          >
            ×
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

function ConfirmModal({
  opts,
  onClose,
}: {
  opts: ConfirmOpts;
  onClose: (v: boolean) => void;
}) {
  const tone = opts.tone ?? "accent";
  const confirmRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    confirmRef.current?.focus();
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose(false);
      } else if (e.key === "Enter") {
        e.preventDefault();
        onClose(true);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  return (
    <Shell
      tone={tone}
      title={opts.title ?? "confirm"}
      onCancel={() => onClose(false)}
    >
      <p className="text-sm text-text leading-relaxed whitespace-pre-wrap">
        {opts.message}
      </p>
      <div className="flex justify-end gap-2 pt-3 border-t border-border/40">
        <button
          onClick={() => onClose(false)}
          className="border border-border text-text-dim hover:text-text hover:border-accent/40 px-3 py-1.5 text-[11px] uppercase tracking-widest"
        >
          {opts.cancelLabel ?? "cancel"}
        </button>
        <button
          ref={confirmRef}
          onClick={() => onClose(true)}
          className={
            "border px-3 py-1.5 text-[11px] uppercase tracking-widest font-semibold " +
            toneBtnClass(tone)
          }
        >
          {opts.confirmLabel ?? "confirm"}
        </button>
      </div>
    </Shell>
  );
}

function PromptModal({
  opts,
  onClose,
}: {
  opts: PromptOpts;
  onClose: (v: string | null) => void;
}) {
  const tone = opts.tone ?? "accent";
  const [value, setValue] = useState(opts.defaultValue ?? "");
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose(null);
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const submit = () => {
    const trimmed = value.trim();
    onClose(trimmed === "" ? null : trimmed);
  };

  return (
    <Shell
      tone={tone}
      title={opts.title ?? "input"}
      onCancel={() => onClose(null)}
    >
      <p className="text-sm text-text leading-relaxed">{opts.message}</p>
      <input
        ref={inputRef}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            submit();
          }
        }}
        placeholder={opts.placeholder}
        className="w-full bg-bg-raised border border-border px-3 py-2 text-sm text-text tabular"
      />
      <div className="flex justify-end gap-2 pt-3 border-t border-border/40">
        <button
          onClick={() => onClose(null)}
          className="border border-border text-text-dim hover:text-text hover:border-accent/40 px-3 py-1.5 text-[11px] uppercase tracking-widest"
        >
          {opts.cancelLabel ?? "cancel"}
        </button>
        <button
          onClick={submit}
          className={
            "border px-3 py-1.5 text-[11px] uppercase tracking-widest font-semibold " +
            toneBtnClass(tone)
          }
        >
          {opts.confirmLabel ?? "submit"}
        </button>
      </div>
    </Shell>
  );
}
