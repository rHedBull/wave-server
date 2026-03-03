import type { Metadata } from "next";
import "@cloudscape-design/global-styles/index.css";
import AgentationOverlay from "@/components/Agentation";

export const metadata: Metadata = {
  title: "Wave Server",
  description: "Wave orchestration dashboard",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body>
        {children}
        <AgentationOverlay />
      </body>
    </html>
  );
}
