import type { RunStatus } from "@/lib/types";

// Status always ships with an icon and a label, never color alone.
const STYLES: Record<RunStatus, { cls: string; icon: string; label: string }> = {
  ok: { cls: "badge-good", icon: "✓", label: "Published" },
  validated: { cls: "badge-good", icon: "✓", label: "Validated" },
  dry_run: { cls: "", icon: "◌", label: "Dry run" },
  partial: { cls: "badge-warning", icon: "!", label: "Partial" },
  failed: { cls: "badge-critical", icon: "✕", label: "Failed" },
  unknown: { cls: "", icon: "?", label: "In progress" },
};

export function StatusBadge({ status }: { status: RunStatus }) {
  const s = STYLES[status];
  return (
    <span className={`badge ${s.cls}`} title={`status: ${status}`}>
      <span aria-hidden="true">{s.icon}</span>
      {s.label}
    </span>
  );
}

export function CheckBadge({ ok, okLabel = "Pass", failLabel = "Fail" }: { ok: boolean; okLabel?: string; failLabel?: string }) {
  return (
    <span className={`badge ${ok ? "badge-good" : "badge-critical"}`}>
      <span aria-hidden="true">{ok ? "✓" : "✕"}</span>
      {ok ? okLabel : failLabel}
    </span>
  );
}
