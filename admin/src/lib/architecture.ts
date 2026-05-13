/**
 * Architecture snapshot types — mirrors
 * `src/data/architecture-snapshots.json`.
 *
 * The data source is a flat JSON bundled with the build. Each snapshot is
 * a frozen point-in-time view of the BIMA system, append-only over the
 * lifetime of the project. The Architecture page reads from here only —
 * no remote fetch, no mutation UI.
 */
import snapshotsData from "@/data/architecture-snapshots.json";

export type ComponentKind =
  | "external"
  | "edge"
  | "service"
  | "data"
  | "frontend"
  | "llm";

export type ArchComponent = {
  id: string;
  label: string;
  kind: ComponentKind;
  subtitle?: string;
  x: number;
  y: number;
};

export type ArchStep = {
  from: string;
  to: string;
  label: string;
  payload: string;
};

export type ArchFlow = {
  id: string;
  label: string;
  summary: string;
  steps: ArchStep[];
};

export type ArchSnapshot = {
  id: string;
  label: string;
  date: string;
  summary: string;
  components: ArchComponent[];
  flows: ArchFlow[];
};

type SnapshotsFile = { snapshots: ArchSnapshot[] };

/**
 * Returns snapshots sorted newest first by `date`. Falling back to the
 * file's natural order if two snapshots share a date.
 */
export function loadSnapshots(): ArchSnapshot[] {
  const file = snapshotsData as SnapshotsFile;
  return [...file.snapshots].sort((a, b) => {
    if (a.date === b.date) return 0;
    return a.date > b.date ? -1 : 1;
  });
}
