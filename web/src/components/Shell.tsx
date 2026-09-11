import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

const NAV = [
  { to: "/optimize", label: "Optimize" },
  { to: "/inspect", label: "Inspect" },
  { to: "/compare", label: "Compare" },
  { to: "/spend", label: "Spend" },
] as const;

export default function Shell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="rule-b flex items-baseline gap-8 px-6 py-3">
        <span className="text-head font-medium tracking-tight">tokop</span>
        <nav className="flex gap-5 text-base">
          {NAV.map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              className={({ isActive }) =>
                isActive ? "text-ink font-medium" : "text-graphite hover:text-ink"
              }
            >
              {n.label}
            </NavLink>
          ))}
        </nav>
        <div className="ml-auto flex items-baseline gap-5">
          <NavLink
            to="/settings"
            className={({ isActive }) =>
              isActive ? "text-ink font-medium" : "text-graphite hover:text-ink"
            }
          >
            Settings
          </NavLink>
        </div>
      </header>
      <main className="flex-1">{children}</main>
    </div>
  );
}
