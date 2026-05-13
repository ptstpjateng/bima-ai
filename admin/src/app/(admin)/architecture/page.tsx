"use client";

import { useCallback, useMemo, useState } from "react";
import { ReactFlowProvider } from "@xyflow/react";
import { Network } from "lucide-react";

import { ArchitectureSidebar } from "@/components/architecture/Sidebar";
import { Canvas } from "@/components/architecture/Canvas";
import { StepNavigator } from "@/components/architecture/StepNavigator";
import { loadSnapshots, type ArchFlow } from "@/lib/architecture";

/**
 * Architecture Flows — read-only visualization of the BIMA system with
 * versioned snapshots and clickable flow annotations. JSON-driven; the
 * data is bundled with the build at `src/data/architecture-snapshots.json`.
 *
 * Layout
 * ─ Title
 * ─ Two-column body:
 *    · Left  (w-72): snapshot picker → flow list → step navigator
 *    · Right (flex): React Flow canvas with our custom BimaNode cards
 */
export default function ArchitecturePage() {
  // Snapshots come from a bundled JSON, sorted newest-first.
  const snapshots = useMemo(() => loadSnapshots(), []);
  const [snapshotId, setSnapshotId] = useState<string>(snapshots[0]?.id ?? "");
  const snapshot = useMemo(
    () => snapshots.find((s) => s.id === snapshotId) ?? snapshots[0],
    [snapshots, snapshotId]
  );

  const [selectedFlowId, setSelectedFlowId] = useState<string | null>(null);
  const [stepIndex, setStepIndex] = useState(0);

  const selectedFlow: ArchFlow | null = useMemo(() => {
    if (!selectedFlowId || !snapshot) return null;
    return snapshot.flows.find((f) => f.id === selectedFlowId) ?? null;
  }, [snapshot, selectedFlowId]);

  const handleSnapshotChange = useCallback((id: string) => {
    setSnapshotId(id);
    setSelectedFlowId(null);
    setStepIndex(0);
  }, []);

  const handleFlowSelect = useCallback((id: string) => {
    setSelectedFlowId((prev) => (prev === id ? null : id));
    setStepIndex(0);
  }, []);

  const handleDeselect = useCallback(() => {
    setSelectedFlowId(null);
    setStepIndex(0);
  }, []);

  const handlePrev = useCallback(() => {
    setStepIndex((i) => Math.max(0, i - 1));
  }, []);

  const handleNext = useCallback(() => {
    if (!selectedFlow) return;
    setStepIndex((i) => Math.min(selectedFlow.steps.length - 1, i + 1));
  }, [selectedFlow]);

  if (!snapshot) {
    return (
      <div className="p-8 text-text-secondary">
        Tidak ada snapshot arsitektur tersedia.
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.14em] text-text-muted">
          <Network className="size-3.5" aria-hidden />
          Arsitektur
        </div>
        <h1 className="font-display text-3xl font-semibold tracking-tight text-text-primary">
          Architecture Flows
        </h1>
        <p className="text-sm text-text-secondary max-w-2xl">
          Peta sistem BIMA yang hidup. Klik sebuah alur untuk menyorot
          komponen yang terlibat dan menelusuri payload-nya langkah demi
          langkah. Snapshot lama tetap tersimpan agar progres arsitektur
          dapat ditelusuri seiring sprint.
        </p>
      </header>

      <div className="flex gap-4 min-h-[640px]">
        <div className="flex flex-col gap-4 w-72 shrink-0">
          <ArchitectureSidebar
            snapshots={snapshots}
            snapshot={snapshot}
            onSnapshotChange={handleSnapshotChange}
            selectedFlowId={selectedFlowId}
            onFlowSelect={handleFlowSelect}
          />
          {selectedFlow && (
            <StepNavigator
              flow={selectedFlow}
              currentIndex={stepIndex}
              components={snapshot.components}
              onPrev={handlePrev}
              onNext={handleNext}
            />
          )}
        </div>

        <div className="flex-1 rounded-card bg-surface-card overflow-hidden">
          <ReactFlowProvider>
            <Canvas
              snapshot={snapshot}
              selectedFlow={selectedFlow}
              currentStepIndex={stepIndex}
              onDeselect={handleDeselect}
            />
          </ReactFlowProvider>
        </div>
      </div>
    </div>
  );
}
