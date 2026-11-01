import { useEffect, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { get, kickWorker } from "../lib/api";
import { useAuth } from "../lib/auth";

const HOME_PATH: Record<string, string> = {
  STUDENT: "/student",
  INDEPENDENT: "/student",
  TEACHER: "/teacher",
  PARENT: "/parent",
  ADMIN: "/admin",
};
const HOME_NAME: Record<string, string> = {
  STUDENT: "Student",
  INDEPENDENT: "My learning",
  TEACHER: "Teacher",
  PARENT: "Parent",
  ADMIN: "School admin",
};

const NAV: Record<string, { to: string; text: string }[]> = {
  student: [
    { to: "/student", text: "Home" },
    { to: "/student/learning", text: "My learning" },
    { to: "/student/progress", text: "Progress" },
    { to: "/student/self", text: "Learn something" },
  ],
  teacher: [
    { to: "/teacher", text: "Home" },
    { to: "/teacher/lessons/new", text: "New lesson" },
    { to: "/teacher/assignments", text: "Assignments" },
    { to: "/teacher/review", text: "Needs attention" },
  ],
  parent: [{ to: "/parent", text: "My children" }],
  admin: [
    { to: "/admin", text: "Home" },
    { to: "/admin/structure", text: "Classes & subjects" },
    { to: "/admin/students", text: "Students" },
    { to: "/admin/teachers", text: "Teachers" },
    { to: "/admin/import", text: "Import" },
    { to: "/admin/rollover", text: "New year" },
  ],
};

export function homePath(homes: string[]): string {
  return HOME_PATH[homes[0]] || "/welcome";
}

export default function Layout() {
  const { me, signOut } = useAuth();
  const loc = useLocation();
  const nav = useNavigate();
  const [unread, setUnread] = useState(0);
  const [menu, setMenu] = useState(false);
  const area = loc.pathname.split("/")[1] || "";
  const links = NAV[area] || [];

  useEffect(() => kickWorker(), []); // run anything queued while nobody was online

  useEffect(() => {
    let alive = true;
    const load = () =>
      get<{ unread_count: number }>("/v1/me/notifications?status=UNREAD&limit=1")
        .then((d) => alive && setUnread(d.unread_count))
        .catch(() => {});
    load();
    const t = window.setInterval(load, 60_000);
    return () => {
      alive = false;
      window.clearInterval(t);
    };
  }, [loc.pathname]);

  const homes = Array.from(new Set(me?.homes || []));
  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-inner">
          <button className="brand" onClick={() => nav(homePath(homes))} aria-label="Aayra home">
            <span className="brand-mark">A</span> Aayra
          </button>
          {homes.length > 1 ? (
            <select
              className="role-switch"
              aria-label="Switch role"
              value={homes.find((h) => HOME_PATH[h] === "/" + area) || homes[0]}
              onChange={(e) => nav(HOME_PATH[e.target.value])}
            >
              {homes.map((h) => (
                <option key={h} value={h}>{HOME_NAME[h]}</option>
              ))}
            </select>
          ) : null}
          <div className="spacer" />
          <NavLink to="/notifications" className="bell" aria-label={`Notifications${unread ? `, ${unread} unread` : ""}`}>
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden><path fill="currentColor" d="M12 22a2.5 2.5 0 0 0 2.45-2h-4.9A2.5 2.5 0 0 0 12 22Zm7-6V11a7 7 0 0 0-5.5-6.84V3.5a1.5 1.5 0 0 0-3 0v.66A7 7 0 0 0 5 11v5l-2 2v1h18v-1z"/></svg>
            {unread ? <span className="bell-count">{unread > 99 ? "99+" : unread}</span> : null}
          </NavLink>
          <div className="menu">
            <button className="avatar" onClick={() => setMenu(!menu)} aria-expanded={menu} aria-label="Account menu">
              {(me?.display_name || "?").slice(0, 1).toUpperCase()}
            </button>
            {menu ? (
              <div className="menu-pop" onMouseLeave={() => setMenu(false)}>
                <div className="menu-name">{me?.display_name}</div>
                {me?.email ? <div className="muted small">{me.email}</div> : null}
                <button className="link" onClick={() => signOut()}>Sign out</button>
              </div>
            ) : null}
          </div>
        </div>
        {links.length ? (
          <nav className="tabs" aria-label="Sections">
            {links.map((l) => (
              <NavLink key={l.to} to={l.to} end className={({ isActive }) => (isActive ? "tab active" : "tab")}>
                {l.text}
              </NavLink>
            ))}
          </nav>
        ) : null}
      </header>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
