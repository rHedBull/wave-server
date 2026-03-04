"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import AppLayout from "@cloudscape-design/components/app-layout";
import SideNavigation, {
  type SideNavigationProps,
} from "@cloudscape-design/components/side-navigation";
import BreadcrumbGroup, {
  type BreadcrumbGroupProps,
} from "@cloudscape-design/components/breadcrumb-group";
import { api, type Project } from "@/lib/api";

interface AppShellProps {
  children: React.ReactNode;
  breadcrumbs?: BreadcrumbGroupProps.Item[];
  activeHref?: string;
  splitPanel?: React.ReactNode;
  splitPanelOpen?: boolean;
  onSplitPanelToggle?: (open: boolean) => void;
}

export default function AppShell({
  children,
  breadcrumbs = [],
  activeHref,
  splitPanel,
  splitPanelOpen,
  onSplitPanelToggle,
}: AppShellProps) {
  const router = useRouter();
  const pathname = usePathname();
  const [projects, setProjects] = useState<Project[]>([]);
  const [version, setVersion] = useState<string | null>(null);
  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {});
    api.getHealth().then((h) => setVersion(h.version)).catch(() => {});
  }, []);

  const navItems: SideNavigationProps.Item[] = [
    {
      type: "link",
      text: "Home",
      href: "/",
    },
    {
      type: "link",
      text: "Projects",
      href: "/projects",
    },
    { type: "divider" },
    ...projects.map(
      (p): SideNavigationProps.Item => ({
        type: "link",
        text: p.name,
        href: `/projects/${p.id}`,
      })
    ),
  ];

  const allBreadcrumbs: BreadcrumbGroupProps.Item[] = [
    { text: "Home", href: "/" },
    ...breadcrumbs,
  ];

  return (
    <AppLayout
      navigationOpen={true}
      onNavigationChange={() => {}}
      navigation={
        <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
          <div style={{ flex: 1 }}>
            <SideNavigation
              header={{ text: "Wave Server", href: "/" }}
              items={navItems}
              activeHref={activeHref || pathname}
              onFollow={(e) => {
                e.preventDefault();
                router.push(e.detail.href);
              }}
            />
          </div>
          {version && (
            <div
              style={{
                padding: "8px 20px 12px",
                fontSize: "12px",
                color: "#687078",
              }}
            >
              v{version}
            </div>
          )}
        </div>
      }
      breadcrumbs={
        <BreadcrumbGroup
          items={allBreadcrumbs}
          onFollow={(e) => {
            e.preventDefault();
            router.push(e.detail.href);
          }}
        />
      }
      content={children}
      splitPanel={splitPanel}
      splitPanelOpen={splitPanelOpen ?? false}
      onSplitPanelToggle={({ detail }) => onSplitPanelToggle?.(detail.open)}
      splitPanelPreferences={{ position: "side" }}
      onSplitPanelPreferencesChange={() => {}}
      toolsHide
    />
  );
}
