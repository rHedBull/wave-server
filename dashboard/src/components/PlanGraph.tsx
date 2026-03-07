"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import Box from "@cloudscape-design/components/box";
import ExpandableSection from "@cloudscape-design/components/expandable-section";
import Header from "@cloudscape-design/components/header";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import type {
  PlanGraph,
  PlanGraphTask,
  PlanGraphWave,
} from "@/lib/api";

// ── Layout constants ─────────────────────────────────────────────

const NODE_W = 180;
const NODE_H = 48;
const NODE_PAD_X = 24;
const NODE_PAD_Y = 16;
const SECTION_PAD_Y = 12;
const SECTION_HEADER_H = 28;
const FEATURE_GAP = 16;
const GRAPH_PAD = 16;

// ── Colors ───────────────────────────────────────────────────────

const COLORS = {
  foundation: { bg: "#e8f4fd", border: "#0972d3", text: "#0972d3", headerBg: "#d1e8fa" },
  feature: { bg: "#f2f8f0", border: "#037f0c", text: "#037f0c", headerBg: "#dff0d8" },
  integration: { bg: "#fdf3e8", border: "#d97706", text: "#d97706", headerBg: "#fde8c8" },
  edge: "#8d99a8",
  node: { bg: "#ffffff", border: "#d1d5db", hoverBg: "#f3f6f9", text: "#16191f" },
  status: {
    completed: { bg: "#f2f8f0", border: "#037f0c", icon: "✓", iconColor: "#037f0c" },
    running:   { bg: "#e8f4fd", border: "#0972d3", icon: "⟳", iconColor: "#0972d3" },
    failed:    { bg: "#fdf0ef", border: "#d91515", icon: "✗", iconColor: "#d91515" },
    skipped:   { bg: "#f4f4f4", border: "#8d99a8", icon: "⊘", iconColor: "#8d99a8" },
    pending:   { bg: "#ffffff", border: "#d1d5db", icon: "",  iconColor: "#d1d5db" },
  } as Record<string, { bg: string; border: string; icon: string; iconColor: string }>,
};

// ── Types ────────────────────────────────────────────────────────

interface NodePos {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
  task: PlanGraphTask;
  section: "foundation" | "feature" | "integration";
  featureName?: string;
}

interface Edge {
  from: string;
  to: string;
}

interface TooltipData {
  task: PlanGraphTask;
  section: string;
  featureName?: string;
  status?: string;
  x: number;
  y: number;
}

// ── Single-wave layout computation ───────────────────────────────

function computeWaveLayout(wave: PlanGraphWave, allTaskIds?: Set<string>) {
  const nodes: NodePos[] = [];
  const edges: Edge[] = [];

  // Foundation: single column
  const foundationRows = wave.foundation.length;
  const foundationH =
    foundationRows > 0
      ? SECTION_HEADER_H + foundationRows * (NODE_H + NODE_PAD_Y) + SECTION_PAD_Y
      : 0;

  // Features: side by side
  const featureWidths: number[] = [];
  let maxFeatureRows = 0;
  for (const feat of wave.features) {
    featureWidths.push(NODE_W);
    maxFeatureRows = Math.max(maxFeatureRows, feat.tasks.length);
  }
  const featuresH =
    wave.features.length > 0
      ? SECTION_HEADER_H +
        maxFeatureRows * (NODE_H + NODE_PAD_Y) +
        SECTION_PAD_Y
      : 0;

  // Integration: single column
  const integrationRows = wave.integration.length;
  const integrationH =
    integrationRows > 0
      ? SECTION_HEADER_H +
        integrationRows * (NODE_H + NODE_PAD_Y) +
        SECTION_PAD_Y
      : 0;

  // Total width: max of foundation, features, integration
  const featTotalW =
    featureWidths.reduce((a, b) => a + b, 0) +
    Math.max(0, wave.features.length - 1) * FEATURE_GAP;
  const contentW = Math.max(NODE_W, featTotalW);
  const totalW = contentW + 2 * GRAPH_PAD;

  let sectionY = GRAPH_PAD;

  // Section rects
  const sectionRects: {
    section: "foundation" | "features" | "integration";
    x: number;
    y: number;
    w: number;
    h: number;
    label: string;
  }[] = [];

  // Foundation
  if (wave.foundation.length > 0) {
    sectionRects.push({
      section: "foundation",
      x: GRAPH_PAD - 4,
      y: sectionY,
      w: contentW + 8,
      h: foundationH,
      label: "Foundation",
    });
    const startY = sectionY + SECTION_HEADER_H;
    for (let i = 0; i < wave.foundation.length; i++) {
      const t = wave.foundation[i];
      const nx = GRAPH_PAD + (contentW - NODE_W) / 2;
      const ny = startY + i * (NODE_H + NODE_PAD_Y);
      nodes.push({
        id: t.id,
        x: nx,
        y: ny,
        w: NODE_W,
        h: NODE_H,
        task: t,
        section: "foundation",
      });
    }
  }
  sectionY += foundationH;

  // Features
  if (wave.features.length > 0) {
    const featureNames = wave.features.map((f) => f.name);
    sectionRects.push({
      section: "features",
      x: GRAPH_PAD - 4,
      y: sectionY,
      w: contentW + 8,
      h: featuresH,
      label:
        featureNames.length === 1 && featureNames[0] === "default"
          ? "Tasks"
          : `Features: ${featureNames.join(", ")}`,
    });
    let featX = GRAPH_PAD + (contentW - featTotalW) / 2;
    for (let fi = 0; fi < wave.features.length; fi++) {
      const feat = wave.features[fi];
      const startY = sectionY + SECTION_HEADER_H;
      for (let ti = 0; ti < feat.tasks.length; ti++) {
        const t = feat.tasks[ti];
        nodes.push({
          id: t.id,
          x: featX,
          y: startY + ti * (NODE_H + NODE_PAD_Y),
          w: NODE_W,
          h: NODE_H,
          task: t,
          section: "feature",
          featureName: feat.name,
        });
      }
      featX += featureWidths[fi] + FEATURE_GAP;
    }
  }
  sectionY += featuresH;

  // Integration
  if (wave.integration.length > 0) {
    sectionRects.push({
      section: "integration",
      x: GRAPH_PAD - 4,
      y: sectionY,
      w: contentW + 8,
      h: integrationH,
      label: "Integration",
    });
    const startY = sectionY + SECTION_HEADER_H;
    for (let i = 0; i < wave.integration.length; i++) {
      const t = wave.integration[i];
      const nx = GRAPH_PAD + (contentW - NODE_W) / 2;
      const ny = startY + i * (NODE_H + NODE_PAD_Y);
      nodes.push({
        id: t.id,
        x: nx,
        y: ny,
        w: NODE_W,
        h: NODE_H,
        task: t,
        section: "integration",
      });
    }
  }

  const totalH = sectionY + integrationH + GRAPH_PAD;

  // Build edges from explicit depends (only within this wave's tasks)
  const nodeMap = new Map(nodes.map((n) => [n.id, n]));
  const waveTaskIds = new Set(nodes.map((n) => n.id));
  for (const node of nodes) {
    for (const depId of node.task.depends) {
      if (nodeMap.has(depId)) {
        edges.push({ from: depId, to: node.id });
      }
    }
  }

  // Build implicit flow edges between sections
  const flowEdges: { from: string; to: string }[] = [];
  if (wave.foundation.length > 0) {
    const lastFoundation = wave.foundation[wave.foundation.length - 1];
    if (wave.features.length > 0) {
      for (const feat of wave.features) {
        if (feat.tasks.length > 0) {
          flowEdges.push({ from: lastFoundation.id, to: feat.tasks[0].id });
        }
      }
    } else if (wave.integration.length > 0) {
      flowEdges.push({ from: lastFoundation.id, to: wave.integration[0].id });
    }
  }
  if (wave.integration.length > 0 && wave.features.length > 0) {
    for (const feat of wave.features) {
      if (feat.tasks.length > 0) {
        const lastFeatTask = feat.tasks[feat.tasks.length - 1];
        flowEdges.push({ from: lastFeatTask.id, to: wave.integration[0].id });
      }
    }
  }

  // Count cross-wave dependencies (deps from other waves)
  const crossWaveDeps: { taskId: string; depId: string }[] = [];
  if (allTaskIds) {
    for (const node of nodes) {
      for (const depId of node.task.depends) {
        if (!waveTaskIds.has(depId) && allTaskIds.has(depId)) {
          crossWaveDeps.push({ taskId: node.id, depId });
        }
      }
    }
  }

  return { nodes, edges, flowEdges, sectionRects, totalW, totalH, nodeMap, crossWaveDeps };
}

// ── Edge path computation ────────────────────────────────────────

function edgePath(
  from: NodePos,
  to: NodePos,
  isFlow: boolean,
): string {
  const fx = from.x + from.w / 2;
  const fy = from.y + from.h;
  const tx = to.x + to.w / 2;
  const ty = to.y;

  if (isFlow) {
    const dy = ty - fy;
    if (Math.abs(fx - tx) < 2) {
      return `M ${fx} ${fy} L ${tx} ${ty}`;
    }
    return `M ${fx} ${fy} C ${fx} ${fy + dy * 0.5}, ${tx} ${ty - dy * 0.5}, ${tx} ${ty}`;
  }

  const dy = ty - fy;
  if (dy > 0) {
    return `M ${fx} ${fy} C ${fx} ${fy + dy * 0.4}, ${tx} ${ty - dy * 0.4}, ${tx} ${ty}`;
  }
  const offset = 30;
  return `M ${fx} ${fy} C ${fx + offset} ${fy + offset}, ${tx - offset} ${ty - offset}, ${tx} ${ty}`;
}

// ── Arrowhead marker ─────────────────────────────────────────────

function ArrowDefs() {
  return (
    <defs>
      <marker
        id="arrow"
        viewBox="0 0 10 10"
        refX="9"
        refY="5"
        markerWidth="8"
        markerHeight="8"
        orient="auto-start-reverse"
      >
        <path d="M 0 0 L 10 5 L 0 10 z" fill={COLORS.edge} />
      </marker>
      <marker
        id="arrow-flow"
        viewBox="0 0 10 10"
        refX="9"
        refY="5"
        markerWidth="7"
        markerHeight="7"
        orient="auto-start-reverse"
      >
        <path d="M 0 0 L 10 5 L 0 10 z" fill={COLORS.edge} opacity={0.4} />
      </marker>
    </defs>
  );
}

// ── Task node component ──────────────────────────────────────────

function TaskNode({
  node,
  onHover,
  onLeave,
  onClick,
  hovered,
  highlighted,
  status,
}: {
  node: NodePos;
  onHover: (node: NodePos, e: React.MouseEvent) => void;
  onLeave: () => void;
  onClick?: (taskId: string) => void;
  hovered: boolean;
  highlighted: boolean;
  status?: string;
}) {
  const sectionColor = COLORS[node.section];
  const opacity = highlighted ? 1 : 0.4;
  const st = status && COLORS.status[status] ? COLORS.status[status] : null;

  const nodeBg = st ? st.bg : COLORS.node.bg;
  const nodeBorder = st ? st.border : COLORS.node.border;
  const accentColor = st ? st.border : sectionColor.border;

  return (
    <g
      onMouseEnter={(e) => onHover(node, e)}
      onMouseLeave={onLeave}
      onClick={onClick ? () => onClick(node.id) : undefined}
      style={{ cursor: onClick ? "pointer" : "default", opacity }}
    >
      <rect
        x={node.x}
        y={node.y}
        width={node.w}
        height={node.h}
        rx={8}
        ry={8}
        fill={hovered ? COLORS.node.hoverBg : nodeBg}
        stroke={hovered ? sectionColor.border : nodeBorder}
        strokeWidth={hovered ? 2 : 1}
      />
      {/* Left accent bar */}
      <rect
        x={node.x}
        y={node.y}
        width={4}
        height={node.h}
        rx={2}
        fill={accentColor}
      />
      {/* Status icon (right side) */}
      {st && st.icon && (
        <text
          x={node.x + node.w - 20}
          y={node.y + 27}
          fontSize={16}
          fontWeight={700}
          fontFamily="system-ui, -apple-system, sans-serif"
          fill={st.iconColor}
          textAnchor="middle"
        >
          {st.icon}
        </text>
      )}
      {/* Running pulse animation */}
      {status === "running" && (
        <circle
          cx={node.x + node.w - 20}
          cy={node.y + 24}
          r={10}
          fill={COLORS.status.running.iconColor}
          opacity={0.15}
        >
          <animate
            attributeName="r"
            values="8;12;8"
            dur="1.5s"
            repeatCount="indefinite"
          />
          <animate
            attributeName="opacity"
            values="0.15;0.05;0.15"
            dur="1.5s"
            repeatCount="indefinite"
          />
        </circle>
      )}
      {/* Task ID badge */}
      <text
        x={node.x + 12}
        y={node.y + 18}
        fontSize={11}
        fontWeight={600}
        fontFamily="system-ui, -apple-system, sans-serif"
        fill={sectionColor.text}
      >
        {node.task.id}
      </text>
      {/* Task title (truncated) */}
      <text
        x={node.x + 12}
        y={node.y + 34}
        fontSize={11}
        fontFamily="system-ui, -apple-system, sans-serif"
        fill={COLORS.node.text}
      >
        {node.task.title.length > 22
          ? node.task.title.slice(0, 20) + "…"
          : node.task.title}
      </text>
    </g>
  );
}

// ── Wave graph (single wave SVG) ─────────────────────────────────

function WaveGraph({
  wave,
  allTaskIds,
  taskStatuses,
  onSelectTask,
}: {
  wave: PlanGraphWave;
  allTaskIds?: Set<string>;
  taskStatuses?: TaskStatusMap;
  onSelectTask?: (taskId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<TooltipData | null>(null);
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  const layout = useMemo(() => computeWaveLayout(wave, allTaskIds), [wave, allTaskIds]);

  const connectedIds = useMemo(() => {
    if (!hoveredId) return null;
    const ids = new Set<string>([hoveredId]);
    const walkUp = (id: string) => {
      for (const e of layout.edges) {
        if (e.to === id && !ids.has(e.from)) {
          ids.add(e.from);
          walkUp(e.from);
        }
      }
      for (const e of layout.flowEdges) {
        if (e.to === id && !ids.has(e.from)) {
          ids.add(e.from);
          walkUp(e.from);
        }
      }
    };
    const walkDown = (id: string) => {
      for (const e of layout.edges) {
        if (e.from === id && !ids.has(e.to)) {
          ids.add(e.to);
          walkDown(e.to);
        }
      }
      for (const e of layout.flowEdges) {
        if (e.from === id && !ids.has(e.to)) {
          ids.add(e.to);
          walkDown(e.to);
        }
      }
    };
    walkUp(hoveredId);
    walkDown(hoveredId);
    return ids;
  }, [layout, hoveredId]);

  const handleHover = useCallback((node: NodePos, e: React.MouseEvent) => {
    setHoveredId(node.id);
    const rect = containerRef.current?.getBoundingClientRect();
    if (rect) {
      setTooltip({
        task: node.task,
        section: node.section,
        featureName: node.featureName,
        status: taskStatuses?.[node.id],
        x: e.clientX - rect.left,
        y: e.clientY - rect.top,
      });
    }
  }, [taskStatuses]);

  const handleLeave = useCallback(() => {
    setHoveredId(null);
    setTooltip(null);
  }, []);

  return (
    <div
      ref={containerRef}
      style={{
        position: "relative",
        overflow: "auto",
        border: "1px solid #e9ebed",
        borderRadius: 8,
        background: "#fbfbfb",
      }}
    >
      <svg
        width={layout.totalW}
        height={layout.totalH}
        style={{ display: "block", margin: "0 auto" }}
      >
        <ArrowDefs />

        {/* Section backgrounds */}
        {layout.sectionRects.map((sr) => {
          const color =
            sr.section === "foundation"
              ? COLORS.foundation
              : sr.section === "integration"
              ? COLORS.integration
              : COLORS.feature;
          return (
            <g key={`section-${sr.section}`}>
              <rect
                x={sr.x}
                y={sr.y}
                width={sr.w}
                height={sr.h}
                rx={8}
                ry={8}
                fill={color.bg}
                stroke={color.border}
                strokeWidth={0.5}
                strokeDasharray="4 2"
                opacity={0.5}
              />
              <text
                x={sr.x + 10}
                y={sr.y + 18}
                fontSize={10}
                fontWeight={600}
                fontFamily="system-ui, -apple-system, sans-serif"
                fill={color.text}
                opacity={0.7}
                style={{ textTransform: "uppercase" }}
              >
                {sr.label}
              </text>
            </g>
          );
        })}

        {/* Flow edges */}
        {layout.flowEdges.map((fe) => {
          const from = layout.nodeMap.get(fe.from);
          const to = layout.nodeMap.get(fe.to);
          if (!from || !to) return null;
          const isHighlighted =
            !hoveredId || (connectedIds?.has(fe.from) && connectedIds?.has(fe.to));
          return (
            <path
              key={`flow-${fe.from}-${fe.to}`}
              d={edgePath(from, to, true)}
              fill="none"
              stroke={COLORS.edge}
              strokeWidth={1}
              strokeDasharray="6 4"
              opacity={isHighlighted ? 0.35 : 0.1}
              markerEnd="url(#arrow-flow)"
            />
          );
        })}

        {/* Dependency edges */}
        {layout.edges.map((edge) => {
          const from = layout.nodeMap.get(edge.from);
          const to = layout.nodeMap.get(edge.to);
          if (!from || !to) return null;
          const isHighlighted =
            !hoveredId || (connectedIds?.has(edge.from) && connectedIds?.has(edge.to));
          return (
            <path
              key={`edge-${edge.from}-${edge.to}`}
              d={edgePath(from, to, false)}
              fill="none"
              stroke={COLORS.edge}
              strokeWidth={1.5}
              opacity={isHighlighted ? 0.8 : 0.15}
              markerEnd="url(#arrow)"
            />
          );
        })}

        {/* Task nodes */}
        {layout.nodes.map((node) => (
          <TaskNode
            key={node.id}
            node={node}
            onHover={handleHover}
            onLeave={handleLeave}
            onClick={onSelectTask}
            hovered={hoveredId === node.id}
            highlighted={!hoveredId || connectedIds?.has(node.id) || false}
            status={taskStatuses?.[node.id]}
          />
        ))}
      </svg>

      {/* Tooltip */}
      {tooltip && (
        <div
          style={{
            position: "absolute",
            left: tooltip.x + 16,
            top: tooltip.y - 8,
            background: "white",
            border: "1px solid #d1d5db",
            borderRadius: 8,
            padding: "12px 16px",
            boxShadow: "0 4px 12px rgba(0,0,0,0.12)",
            maxWidth: 320,
            zIndex: 10,
            pointerEvents: "none",
            fontSize: 12,
            fontFamily: "system-ui, -apple-system, sans-serif",
          }}
        >
          <div style={{ fontWeight: 700, marginBottom: 4, fontSize: 13 }}>
            {tooltip.task.id}: {tooltip.task.title}
          </div>
          <div style={{ color: "#687078", marginBottom: 4 }}>
            <span
              style={{
                display: "inline-block",
                padding: "1px 6px",
                borderRadius: 4,
                fontSize: 10,
                fontWeight: 600,
                background:
                  tooltip.section === "foundation"
                    ? COLORS.foundation.headerBg
                    : tooltip.section === "integration"
                    ? COLORS.integration.headerBg
                    : COLORS.feature.headerBg,
                color:
                  tooltip.section === "foundation"
                    ? COLORS.foundation.text
                    : tooltip.section === "integration"
                    ? COLORS.integration.text
                    : COLORS.feature.text,
                marginRight: 6,
              }}
            >
              {tooltip.section}
            </span>
            {tooltip.featureName && tooltip.featureName !== "default" && (
              <span style={{ color: COLORS.feature.text }}>
                {tooltip.featureName}
              </span>
            )}
          </div>
          {tooltip.status && (
            <div style={{ color: "#687078", marginBottom: 4 }}>
              <span
                style={{
                  display: "inline-block",
                  padding: "1px 6px",
                  borderRadius: 4,
                  fontSize: 10,
                  fontWeight: 600,
                  background: COLORS.status[tooltip.status]?.bg ?? "#f4f4f4",
                  color: COLORS.status[tooltip.status]?.iconColor ?? "#687078",
                  border: `1px solid ${COLORS.status[tooltip.status]?.border ?? "#d1d5db"}`,
                }}
              >
                {COLORS.status[tooltip.status]?.icon} {tooltip.status}
              </span>
            </div>
          )}
          <div style={{ color: "#687078", marginBottom: 2 }}>
            <strong>Agent:</strong> {tooltip.task.agent}
          </div>
          {tooltip.task.files.length > 0 && (
            <div style={{ color: "#687078", marginBottom: 2 }}>
              <strong>Files:</strong>{" "}
              <code style={{ fontSize: 11 }}>
                {tooltip.task.files.join(", ")}
              </code>
            </div>
          )}
          {tooltip.task.depends.length > 0 && (
            <div style={{ color: "#687078" }}>
              <strong>Depends:</strong> {tooltip.task.depends.join(", ")}
            </div>
          )}
        </div>
      )}

      {/* Cross-wave dependencies note */}
      {layout.crossWaveDeps.length > 0 && (
        <Box margin={{ top: "xs" }} padding={{ horizontal: "s" }}>
          <Box variant="small" color="text-body-secondary">
            ↗ Cross-wave deps:{" "}
            {layout.crossWaveDeps.map((d) => `${d.taskId}←${d.depId}`).join(", ")}
          </Box>
        </Box>
      )}
    </div>
  );
}

// ── Compute wave-level status summary ────────────────────────────

function waveStatusSummary(
  wave: PlanGraphWave,
  taskStatuses?: TaskStatusMap,
): { completed: number; running: number; failed: number; total: number; status: string } {
  const allTasks: PlanGraphTask[] = [
    ...wave.foundation,
    ...wave.features.flatMap((f) => f.tasks),
    ...wave.integration,
  ];
  const total = allTasks.length;
  let completed = 0;
  let running = 0;
  let failed = 0;
  for (const t of allTasks) {
    const s = taskStatuses?.[t.id];
    if (s === "completed") completed++;
    else if (s === "running") running++;
    else if (s === "failed") failed++;
  }

  let status = "pending";
  if (failed > 0) status = "failed";
  else if (completed === total && total > 0) status = "completed";
  else if (running > 0 || completed > 0) status = "running";

  return { completed, running, failed, total, status };
}

function waveStatusIndicatorType(status: string) {
  switch (status) {
    case "completed": return "success" as const;
    case "failed": return "error" as const;
    case "running": return "in-progress" as const;
    default: return "pending" as const;
  }
}

// ── Main component ───────────────────────────────────────────────

export interface TaskStatusMap {
  [taskId: string]: string;
}

interface PlanGraphProps {
  graph: PlanGraph | null;
  loading?: boolean;
  taskStatuses?: TaskStatusMap;
  onSelectTask?: (taskId: string) => void;
}

export default function PlanGraphView({ graph, loading, taskStatuses, onSelectTask }: PlanGraphProps) {
  // Determine which waves should be expanded by default
  const defaultExpanded = useMemo(() => {
    if (!graph || !taskStatuses) {
      // No statuses: expand all
      return new Set(graph?.waves.map((_, i) => i) ?? []);
    }
    // Expand waves that are running or failed, or the first pending wave
    const expanded = new Set<number>();
    let foundPending = false;
    for (const wave of graph.waves) {
      const summary = waveStatusSummary(wave, taskStatuses);
      if (summary.status === "running" || summary.status === "failed") {
        expanded.add(wave.index);
      } else if (summary.status === "pending" && !foundPending) {
        expanded.add(wave.index);
        foundPending = true;
      }
    }
    // If nothing is expanded, expand all
    if (expanded.size === 0) {
      for (const wave of graph.waves) expanded.add(wave.index);
    }
    return expanded;
  }, [graph, taskStatuses]);

  const [expandedWaves, setExpandedWaves] = useState<Set<number> | null>(null);

  // Use default on first render, then track user changes
  const expanded = expandedWaves ?? defaultExpanded;

  const toggleWave = useCallback((index: number) => {
    setExpandedWaves((prev) => {
      const current = prev ?? defaultExpanded;
      const next = new Set(current);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  }, [defaultExpanded]);

  // Collect all task IDs across all waves for cross-wave dep detection
  const allTaskIds = useMemo(() => {
    if (!graph) return undefined;
    const ids = new Set<string>();
    for (const wave of graph.waves) {
      for (const t of wave.foundation) ids.add(t.id);
      for (const f of wave.features) for (const t of f.tasks) ids.add(t.id);
      for (const t of wave.integration) ids.add(t.id);
    }
    return ids;
  }, [graph]);

  if (loading) {
    return (
      <SpaceBetween size="s">
        <Header variant="h3">Execution Graph</Header>
        <Box textAlign="center" padding="l">
          <StatusIndicator type="loading">Loading graph…</StatusIndicator>
        </Box>
      </SpaceBetween>
    );
  }

  if (!graph || graph.waves.length === 0) {
    return (
      <SpaceBetween size="s">
        <Header variant="h3">Execution Graph</Header>
        <Box textAlign="center" color="inherit" padding="l">
          <b>No plan available</b>
          <Box variant="p" color="text-body-secondary">
            Upload a plan to see the execution graph.
          </Box>
        </Box>
      </SpaceBetween>
    );
  }

  return (
    <SpaceBetween size="s">
      <Header
        variant="h3"
        description={graph.goal || undefined}
      >
        Execution Graph
      </Header>

      {graph.waves.map((wave) => {
        const summary = taskStatuses
          ? waveStatusSummary(wave, taskStatuses)
          : null;
        const isExpanded = expanded.has(wave.index);

        return (
          <ExpandableSection
            key={wave.index}
            variant="container"
            expanded={isExpanded}
            onChange={() => toggleWave(wave.index)}
            headerText={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
                <span>Wave {wave.index + 1}: {wave.name}</span>
                {summary && (
                  <StatusIndicator type={waveStatusIndicatorType(summary.status)}>
                    {summary.completed}/{summary.total}
                  </StatusIndicator>
                )}
              </span>
            }
            headerDescription={wave.description || undefined}
          >
            <WaveGraph
              wave={wave}
              allTaskIds={allTaskIds}
              taskStatuses={taskStatuses}
              onSelectTask={onSelectTask}
            />
          </ExpandableSection>
        );
      })}

      {/* Legend */}
      <Box>
        <SpaceBetween size="xs">
          <SpaceBetween direction="horizontal" size="l">
            <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
              <span
                style={{
                  width: 12,
                  height: 12,
                  borderRadius: 3,
                  background: COLORS.foundation.border,
                  display: "inline-block",
                }}
              />
              Foundation
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
              <span
                style={{
                  width: 12,
                  height: 12,
                  borderRadius: 3,
                  background: COLORS.feature.border,
                  display: "inline-block",
                }}
              />
              Feature
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
              <span
                style={{
                  width: 12,
                  height: 12,
                  borderRadius: 3,
                  background: COLORS.integration.border,
                  display: "inline-block",
                }}
              />
              Integration
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
              <svg width={24} height={12}>
                <line x1={0} y1={6} x2={24} y2={6} stroke={COLORS.edge} strokeWidth={1.5} />
              </svg>
              Dependency
            </span>
            <span style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
              <svg width={24} height={12}>
                <line
                  x1={0}
                  y1={6}
                  x2={24}
                  y2={6}
                  stroke={COLORS.edge}
                  strokeWidth={1}
                  strokeDasharray="4 3"
                  opacity={0.5}
                />
              </svg>
              Flow
            </span>
          </SpaceBetween>
          {taskStatuses && Object.keys(taskStatuses).length > 0 && (
            <SpaceBetween direction="horizontal" size="l">
              {(["completed", "running", "failed", "skipped", "pending"] as const).map((s) => {
                const st = COLORS.status[s];
                return (
                  <span key={s} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 12 }}>
                    <span
                      style={{
                        width: 16,
                        height: 16,
                        borderRadius: 4,
                        background: st.bg,
                        border: `1px solid ${st.border}`,
                        display: "inline-flex",
                        alignItems: "center",
                        justifyContent: "center",
                        fontSize: 10,
                        fontWeight: 700,
                        color: st.iconColor,
                        lineHeight: 1,
                      }}
                    >
                      {st.icon}
                    </span>
                    {s.charAt(0).toUpperCase() + s.slice(1)}
                  </span>
                );
              })}
            </SpaceBetween>
          )}
        </SpaceBetween>
      </Box>
    </SpaceBetween>
  );
}
