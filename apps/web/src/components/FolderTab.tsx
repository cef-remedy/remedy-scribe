/**
 * The folder-tab itself — a genuine tab silhouette (clip-path trapezoid),
 * not a colored border. Every place this app shows status uses this one
 * component, so "what does terracotta mean here" only has to be learned
 * once (The Patient Folder direction, apps/web/index.html).
 */
import type { TabKind } from "../lib/status-tab";

export function FolderTab({ kind, label }: { kind: TabKind; label: string }) {
  const cls = kind === "blank" ? "folder-tab tab-blank" : `folder-tab tab-${kind}`;
  return <span className={cls}>{label}</span>;
}

/**
 * The lockable one-way step-sequence, raised into this direction from the
 * roll's declined origami-fold candidate: recording → uploaded →
 * transcribed → note_generated → signed. A plain word alone was the thing
 * being replaced, so the label stays too, for anyone not reading dots.
 */
export function StepSequence({
  length,
  index,
  terminal,
  label,
}: {
  length: number;
  index: number;
  terminal: "attention" | "hold" | null;
  label: string;
}) {
  return (
    <div className="step-sequence" role="img" aria-label={label}>
      {Array.from({ length }).map((_, i) => {
        const cls =
          i === index && terminal
            ? `step-cell is-${terminal}`
            : i < index
              ? "step-cell is-filled"
              : i === index
                ? "step-cell is-current"
                : "step-cell";
        return <span key={i} className={cls} aria-hidden="true" />;
      })}
      <span className="step-label">{label}</span>
    </div>
  );
}
