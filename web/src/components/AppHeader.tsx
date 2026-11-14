/** The header: the product name, the screens, and who is signed in. */
export interface NavItem {
  path: string;
  label: string;
}

export function AppHeader({
  currentPath,
  items,
  onNavigate,
  user,
  onLogout,
}: {
  currentPath: string;
  items: NavItem[];
  onNavigate: (path: string) => void;
  user?: { name: string | null; email: string | null } | null;
  onLogout?: () => void;
}) {
  const who = user?.name || user?.email;
  return (
    <header className="clay-bar sticky top-0 z-30 border-b border-line bg-white">
      <div className="mx-auto flex max-w-5xl items-center gap-3 px-4 py-3">
        <span className="text-base font-semibold tracking-tight text-ink">
          Lecture to Learning
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
                    className={`clay-nav clay-btn rounded-md px-3 py-2 text-sm font-medium ${
                      active
                        ? "clay-accent bg-accent text-white"
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
        {who && onLogout && (
          <div className="flex items-center gap-2 border-l border-line pl-3">
            <span
              className="hidden max-w-[10rem] truncate text-sm text-muted sm:block"
              title={who}
            >
              {who}
            </span>
            <button
              type="button"
              onClick={onLogout}
              className="clay-nav clay-btn rounded-md px-3 py-2 text-sm font-medium text-body hover:bg-canvas"
            >
              Sign out
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
