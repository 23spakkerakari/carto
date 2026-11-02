import { NavLink, Outlet } from 'react-router';

import { useLogout, useMe } from '../api/hooks';
import type { Permission } from '../api/types';
import { hasPermission, ROLE_WORDS } from '../lib/words';
import { Button, ErrorLine, Loading } from '../ui/primitives';

interface NavItem {
  to: string;
  label: string;
  needs?: Permission;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const NAV: NavGroup[] = [
  {
    label: 'Map',
    items: [
      { to: '/map/review', label: 'Review' },
      { to: '/map/entities', label: 'Entities' },
    ],
  },
  { label: 'Systems', items: [{ to: '/systems', label: 'Systems' }] },
  {
    label: 'Setup',
    items: [
      { to: '/setup/sources', label: 'Sources' },
      { to: '/setup/fields', label: 'Field policies' },
    ],
  },
  { label: 'Admin', items: [{ to: '/admin/audit', label: 'Audit', needs: 'view_audit' }] },
];

export function Shell() {
  const me = useMe();
  const logout = useLogout();

  if (me.isPending) return <Loading what="Signing in" />;
  if (me.isError) {
    return (
      <main className="mx-auto max-w-3xl p-8">
        <ErrorLine error={me.error} />
      </main>
    );
  }
  const permissions = me.data.permissions;
  const groups = NAV.map((group) => ({
    ...group,
    items: group.items.filter((item) => !item.needs || hasPermission(permissions, item.needs)),
  })).filter((group) => group.items.length > 0);

  return (
    <div className="flex min-h-screen">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-10 focus:bg-hi focus:px-2 focus:py-1"
      >
        Skip to content
      </a>
      <nav
        aria-label="Console"
        className="flex w-52 shrink-0 flex-col border-r border-hairline px-4 py-4"
      >
        <div className="rule-2 pt-2 pb-3">
          <div className="font-cond text-[18px] leading-6 font-semibold tracking-tight">carto</div>
          <div className="label-cond text-muted">console</div>
        </div>
        {groups.map((group) => (
          <div key={group.label} className="mt-4">
            <div className="label-cond hairline pb-1 text-muted">{group.label}</div>
            <ul className="mt-1">
              {group.items.map((item) => (
                <li key={item.to}>
                  <NavLink
                    to={item.to}
                    className={({ isActive }) =>
                      `block py-1 pl-2 font-cond text-[14px] font-medium ${isActive ? 'border-l-2 border-ink bg-hi' : 'border-l-2 border-transparent hover:bg-paper-2'}`
                    }
                  >
                    {item.label}
                  </NavLink>
                </li>
              ))}
            </ul>
          </div>
        ))}
        <div className="mt-auto border-t border-hairline pt-3 text-[13px]">
          <div className="text-break font-medium">{me.data.display_name || me.data.subject}</div>
          <div className="text-muted">
            {me.data.roles.map((role) => ROLE_WORDS[role]).join(', ')}
          </div>
          <Button
            variant="quiet"
            className="mt-2 -ml-3"
            onClick={() => {
              logout.mutate();
            }}
            disabled={logout.isPending}
          >
            Sign out
          </Button>
          <ErrorLine error={logout.error ?? undefined} />
        </div>
      </nav>
      <main id="main" className="min-w-0 flex-1 px-8 py-4">
        <Outlet />
      </main>
    </div>
  );
}
