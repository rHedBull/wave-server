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
}

export default function AppShell({
  children,
  breadcrumbs = [],
  activeHref,
}: AppShellProps) {
  const router = useRouter();
  const pathname = usePathname();
  const [projects, setProjects] = useState<Project[]>([]);

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {});
  }, []);

  const navItems: SideNavigationProps.Item[] = [
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
    { text: "Wave Server", href: "/" },
    ...breadcrumbs,
  ];

  return (
    <AppLayout
      navigation={
        <SideNavigation
          header={{ text: "Wave Server", href: "/" }}
          items={navItems}
          activeHref={activeHref || pathname}
          onFollow={(e) => {
            e.preventDefault();
            router.push(e.detail.href);
          }}
        />
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
      toolsHide
    />
  );
}
