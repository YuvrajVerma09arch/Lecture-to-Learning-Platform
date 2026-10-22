/**
 * The heading navigation.
 *
 * It is a collapsible list of the document's own headings. On a phone
 * it starts closed and sits above the document; on a wide screen it
 * starts open in a sticky column beside it.
 */
import { useEffect, useRef, useState } from "react";
import type { Heading } from "../lib/markdown";

function startsWide(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(min-width: 1024px)").matches
  );
}

export function TableOfContents({ headings }: { headings: Heading[] }) {
  const [open, setOpen] = useState(startsWide);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement>(null);

  // Follow the part of the document being read.
  useEffect(() => {
    if (headings.length === 0) return;
    const elements = headings
      .map((heading) => document.getElementById(heading.id))
      .filter((element): element is HTMLElement => element !== null);
    if (elements.length === 0) return;

    const observer = new IntersectionObserver(
      (records) => {
        const visible = records
          .filter((record) => record.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible.length > 0) setCurrentId(visible[0].target.id);
      },
      { rootMargin: "-10% 0px -75% 0px" },
    );
    elements.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [headings]);

  useEffect(() => {
    if (!currentId || !listRef.current) return;
    const item = listRef.current.querySelector<HTMLElement>(
      `[data-toc-id="${currentId}"]`,
    );
    item?.scrollIntoView({ block: "nearest" });
  }, [currentId]);

  if (headings.length === 0) return null;

  const jump = (id: string) => {
    const target = document.getElementById(id);
    if (!target) return;
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    target.focus({ preventScroll: true });
    setCurrentId(id);
  };

  return (
    <nav
      aria-label="Table of contents"
      className="rounded-lg border border-line bg-white"
    >
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        aria-controls="toc-list"
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm font-semibold text-ink"
      >
        <span>Contents ({headings.length})</span>
        <span aria-hidden="true" className="text-muted">
          {open ? "−" : "+"}
        </span>
      </button>
      <ul
        id="toc-list"
        ref={listRef}
        hidden={!open}
        className="max-h-[55vh] overflow-y-auto border-t border-line px-2 py-2"
      >
        {headings.map((heading) => (
          <li key={heading.id}>
            <button
              type="button"
              data-toc-id={heading.id}
              onClick={() => jump(heading.id)}
              aria-current={currentId === heading.id ? "true" : undefined}
              className={`block w-full truncate rounded px-2 py-1.5 text-left text-sm hover:bg-canvas ${
                heading.level === 1
                  ? "font-semibold text-ink"
                  : heading.level === 2
                    ? "text-body"
                    : "text-muted"
              } ${
                currentId === heading.id
                  ? "bg-canvas font-semibold text-accent-ink"
                  : ""
              }`}
              style={{ paddingLeft: `${0.5 + (heading.level - 1) * 0.75}rem` }}
            >
              {heading.text}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}
