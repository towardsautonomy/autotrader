"use client";

import { useMemo, useState } from "react";

const DEFAULT_PER_PAGE = 10;
const DEFAULT_MAX_PAGES = 10;

type PaginatedState<T> = {
  visible: T[];
  page: number;
  totalPages: number;
  totalKept: number;
  truncated: boolean;
  setPage: (p: number) => void;
  Pager: (() => React.ReactElement | null);
};

export function usePagination<T>(
  items: T[],
  opts: { perPage?: number; maxPages?: number } = {},
): PaginatedState<T> {
  const perPage = opts.perPage ?? DEFAULT_PER_PAGE;
  const maxPages = opts.maxPages ?? DEFAULT_MAX_PAGES;
  const [page, setPage] = useState(0);

  const { visible, totalPages, totalKept, truncated, safePage } = useMemo(() => {
    const cap = perPage * maxPages;
    const kept = items.slice(0, cap);
    const pages = Math.max(1, Math.ceil(kept.length / perPage));
    const safe = Math.min(page, pages - 1);
    const start = safe * perPage;
    return {
      visible: kept.slice(start, start + perPage),
      totalPages: pages,
      totalKept: kept.length,
      truncated: items.length > kept.length,
      safePage: safe,
    };
  }, [items, page, perPage, maxPages]);

  const Pager = () => {
    if (totalPages <= 1) return null;
    return (
      <PagerControls
        page={safePage}
        totalPages={totalPages}
        onPage={setPage}
        truncated={truncated}
        maxPages={maxPages}
      />
    );
  };

  return {
    visible,
    page: safePage,
    totalPages,
    totalKept,
    truncated,
    setPage,
    Pager,
  };
}

export function PagerControls({
  page,
  totalPages,
  onPage,
  truncated,
  maxPages,
}: {
  page: number;
  totalPages: number;
  onPage: (p: number) => void;
  truncated: boolean;
  maxPages: number;
}) {
  const btn =
    "px-2 py-0.5 text-[11px] tabular border border-border/60 " +
    "hover:border-accent/60 hover:text-accent disabled:opacity-30 " +
    "disabled:hover:border-border/60 disabled:hover:text-inherit " +
    "disabled:cursor-not-allowed";
  return (
    <div className="flex items-center justify-between pt-2 text-[11px] text-text-dim flex-wrap gap-2">
      <div className="flex items-center gap-1 flex-wrap">
        <button
          type="button"
          className={btn}
          disabled={page === 0}
          onClick={() => onPage(Math.max(0, page - 1))}
        >
          ‹ prev
        </button>
        {Array.from({ length: totalPages }, (_, i) => (
          <button
            key={i}
            type="button"
            className={`${btn} ${
              i === page ? "text-accent border-accent/60" : ""
            }`}
            onClick={() => onPage(i)}
          >
            {i + 1}
          </button>
        ))}
        <button
          type="button"
          className={btn}
          disabled={page >= totalPages - 1}
          onClick={() => onPage(Math.min(totalPages - 1, page + 1))}
        >
          next ›
        </button>
      </div>
      <span className="text-text-faint">
        page {page + 1} of {totalPages}
        {truncated ? ` · older entries beyond page ${maxPages} are dropped` : ""}
      </span>
    </div>
  );
}
