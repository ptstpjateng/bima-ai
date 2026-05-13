"use client";

import { useEffect, useMemo } from "react";
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";

import "@xyflow/react/dist/style.css";

import { BimaNode, type BimaNodeData } from "./BimaNode";
import type {
  ArchComponent,
  ArchFlow,
  ArchSnapshot,
} from "@/lib/architecture";

const NODE_TYPES = { bima: BimaNode };

const BRAND_AMBER = "#F59E0B";
const EDGE_GRAY = "#374151";

type CanvasProps = {
  snapshot: ArchSnapshot;
  selectedFlow: ArchFlow | null;
  currentStepIndex: number;
  onDeselect: () => void;
};

/**
 * Builds an edge key that distinguishes from→to pairs *regardless* of
 * ordering of steps inside a flow. Used to figure out which edges are
 * involved in a given flow.
 */
function edgeKey(from: string, to: string) {
  return `${from}__${to}`;
}

/**
 * Collect all node ids that participate in any step of a flow, including
 * self-loop annotations (which still need to stay full-opacity).
 */
function flowNodeIds(flow: ArchFlow): Set<string> {
  const set = new Set<string>();
  for (const step of flow.steps) {
    set.add(step.from);
    set.add(step.to);
  }
  return set;
}

function componentToNode(c: ArchComponent, dimmed: boolean): Node<BimaNodeData> {
  return {
    id: c.id,
    type: "bima",
    position: { x: c.x, y: c.y },
    data: {
      label: c.label,
      subtitle: c.subtitle,
      kind: c.kind,
      dimmed,
    },
    draggable: false,
    selectable: false,
    connectable: false,
  };
}

function buildEdges(
  snapshot: ArchSnapshot,
  selectedFlow: ArchFlow | null,
  currentStepIndex: number
): Edge[] {
  // We only render edges for the *currently selected* flow when one is
  // active — otherwise we draw the union of all flow edges in calm gray
  // so the canvas hints at the system shape on first load.
  const edges: Edge[] = [];

  if (selectedFlow) {
    // Walk the steps in order, dedupe by (from,to) but remember the
    // first step index that hit each edge for label/highlighting.
    const seen = new Map<string, number>();
    selectedFlow.steps.forEach((step, idx) => {
      if (step.from === step.to) return;
      const key = edgeKey(step.from, step.to);
      if (!seen.has(key)) seen.set(key, idx);
    });

    const currentStep = selectedFlow.steps[currentStepIndex];
    const currentKey =
      currentStep && currentStep.from !== currentStep.to
        ? edgeKey(currentStep.from, currentStep.to)
        : null;

    for (const [key, firstIdx] of seen.entries()) {
      const [from, to] = key.split("__");
      const step = selectedFlow.steps[firstIdx];
      const isCurrent = key === currentKey;
      edges.push({
        id: `${selectedFlow.id}-${key}`,
        source: from,
        target: to,
        type: "smoothstep",
        animated: !isCurrent, // dashed-march for non-current; current is solid+thick
        label: isCurrent ? step.label : undefined,
        labelStyle: {
          fill: "#F1F5F9",
          fontSize: 11,
          fontWeight: 500,
        },
        labelBgStyle: {
          fill: "#1A2138",
        },
        labelBgPadding: [6, 4],
        labelBgBorderRadius: 6,
        style: {
          stroke: BRAND_AMBER,
          strokeWidth: isCurrent ? 3 : 1.5,
          strokeDasharray: isCurrent ? undefined : "6 4",
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: BRAND_AMBER,
          width: 18,
          height: 18,
        },
      });
    }
    return edges;
  }

  // No flow selected — render the union of all flow edges in gray so
  // the system shape is visible. Dedupe across flows.
  const seenAll = new Set<string>();
  for (const flow of snapshot.flows) {
    for (const step of flow.steps) {
      if (step.from === step.to) continue;
      const key = edgeKey(step.from, step.to);
      if (seenAll.has(key)) continue;
      seenAll.add(key);
      edges.push({
        id: `all-${key}`,
        source: step.from,
        target: step.to,
        type: "smoothstep",
        animated: false,
        style: {
          stroke: EDGE_GRAY,
          strokeWidth: 1,
        },
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: EDGE_GRAY,
          width: 14,
          height: 14,
        },
      });
    }
  }
  // Mark unused so build doesn't complain about flow var capture in dev
  // (currentStepIndex only matters when a flow is selected).
  void currentStepIndex;
  return edges;
}

export function Canvas({
  snapshot,
  selectedFlow,
  currentStepIndex,
  onDeselect,
}: CanvasProps) {
  // Derive nodes/edges from snapshot + selection. We use React Flow's
  // state hooks so internal viewport interactions (pan) don't trigger
  // React re-renders.
  const initialNodes = useMemo<Node<BimaNodeData>[]>(() => {
    const involved = selectedFlow ? flowNodeIds(selectedFlow) : null;
    return snapshot.components.map((c) =>
      componentToNode(c, involved ? !involved.has(c.id) : false)
    );
  }, [snapshot, selectedFlow]);

  const initialEdges = useMemo<Edge[]>(
    () => buildEdges(snapshot, selectedFlow, currentStepIndex),
    [snapshot, selectedFlow, currentStepIndex]
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const { fitView } = useReactFlow();

  // Keep React Flow state in sync with derived values. We do this in an
  // effect rather than passing nodes/edges directly so that React Flow's
  // viewport (pan/zoom) state survives a re-render.
  useEffect(() => {
    setNodes(initialNodes);
  }, [initialNodes, setNodes]);

  useEffect(() => {
    setEdges(initialEdges);
  }, [initialEdges, setEdges]);

  // When the snapshot itself changes (different layout), re-fit. We
  // deliberately don't refit on flow selection — that would feel jumpy.
  useEffect(() => {
    const t = window.setTimeout(() => {
      fitView({ padding: 0.15, duration: 400 });
    }, 0);
    return () => window.clearTimeout(t);
  }, [snapshot.id, fitView]);

  return (
    <div
      className="h-full w-full rounded-card overflow-hidden bg-surface-base relative"
      style={{ minHeight: 560 }}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={NODE_TYPES}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag
        panOnScroll={false}
        zoomOnScroll
        zoomOnPinch
        proOptions={{ hideAttribution: true }}
        onPaneClick={onDeselect}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        defaultEdgeOptions={{ type: "smoothstep" }}
        minZoom={0.3}
        maxZoom={1.6}
        colorMode="dark"
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1}
          color="#1a2138"
        />
        <Controls
          showInteractive={false}
          position="bottom-right"
          className="!bg-surface-card !border-0 !shadow-none [&>button]:!bg-surface-card [&>button]:!border-white/5 [&>button]:!text-text-secondary"
        />
      </ReactFlow>
    </div>
  );
}
