/** The header: the product name and the two screens. */
export interface NavItem {
  path: string;
  label: string;
}

export function AppHeader({
  currentPath,
  items,
  onNavigate,
}: {
  currentPath: string;
  items: NavItem[];
  onNavigate: (path: string) => void;
}) {
  return (
    <header className="sticky top-0 z-30 border-b border-line bg-white">
      <div className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-3">
        <span className="text-base font-semibold tracking-tight text-ink">
          Gnani
        </span>
        <nav aria-label="Main" className="ml-auto">
          <ul className="flex items-center gap-1">
            {items.map((item) => {
              const active = currentPath === item.path;
              return (
                <li key={item.path}>
                  <button
                    type="button"
                    onClick={() => onNavigate(item.path)}
                    aria-current={active ? "page" : undefined}
                    className={`rounded-md px-3 py-2 text-sm font-medium ${
                      active
                        ? "bg-accent text-white"
                        : "text-body hover:bg-canvas"
                    }`}
                  >
                    {item.label}
                  </button>
                </li>
              );
            })}
          </ul>
        </nav>
      </div>
    </header>
  );
}
