"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";

const LINKS = [
  { href: "/", label: "dashboard" },
  { href: "/analytics", label: "analytics" },
  { href: "/intel", label: "intel" },
  { href: "/research", label: "research" },
  { href: "/agents", label: "agents" },
  { href: "/decisions", label: "decisions" },
  { href: "/trades", label: "trades" },
  { href: "/risk-config", label: "risk" },
];

const PT_FORMATTER = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Los_Angeles",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function formatPacific(now: Date): string {
  const parts = Object.fromEntries(
    PT_FORMATTER.formatToParts(now).map((p) => [p.type, p.value]),
  );
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second} PT`;
}

export default function NavBar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [clock, setClock] = useState<string | null>(null);

  useEffect(() => {
    const tick = () => setClock(formatPacific(new Date()));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  const isActive = (href: string) =>
    href === "/" ? pathname === "/" : pathname.startsWith(href);

  return (
    <nav className="border-b border-border bg-bg-panel/80 backdrop-blur-sm relative z-20">
      <div className="max-w-6xl mx-auto px-4 sm:px-6 py-3 flex items-center gap-3 sm:gap-6 text-sm">
        <Link href="/" className="flex items-center gap-2 group shrink-0">
          <span className="text-accent">▸</span>
          <span className="font-semibold tracking-wide text-text group-hover:text-accent transition-colors">
            autotrader
          </span>
        </Link>
        <span className="text-text-faint hidden sm:inline">::</span>

        <div className="hidden sm:flex gap-1 flex-wrap">
          {LINKS.map((l) => {
            const active = isActive(l.href);
            return (
              <Link
                key={l.href}
                href={l.href}
                className={
                  "px-2 py-1 rounded-sm transition-colors " +
                  (active
                    ? "text-accent bg-accent/10"
                    : "text-text-dim hover:text-text")
                }
              >
                {active && <span className="text-accent mr-1">●</span>}
                {l.label}
              </Link>
            );
          })}
        </div>

        <span
          className="hidden sm:inline ml-auto text-text tabular-nums tracking-wider text-[11px] uppercase"
          suppressHydrationWarning
        >
          {clock ?? "—"}
        </span>

        <button
          type="button"
          aria-label="toggle navigation"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="sm:hidden ml-auto border border-border text-text-dim hover:text-accent hover:border-accent/40 px-3 py-1.5 uppercase tracking-widest text-[11px]"
        >
          {open ? "× close" : "≡ menu"}
        </button>
      </div>

      {open && (
        <div className="sm:hidden border-t border-border bg-bg-panel/95 backdrop-blur-sm">
          <div className="max-w-6xl mx-auto px-4 py-2 flex flex-col">
            {LINKS.map((l) => {
              const active = isActive(l.href);
              return (
                <Link
                  key={l.href}
                  href={l.href}
                  onClick={() => setOpen(false)}
                  className={
                    "px-2 py-2.5 rounded-sm transition-colors text-sm " +
                    (active
                      ? "text-accent bg-accent/10"
                      : "text-text-dim hover:text-text")
                  }
                >
                  {active && <span className="text-accent mr-1">●</span>}
                  {l.label}
                </Link>
              );
            })}
          </div>
        </div>
      )}
    </nav>
  );
}
