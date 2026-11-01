export function toDate(v: string | null | undefined): Date | null {
  if (!v) return null;
  const d = new Date(v.includes(" ") && !v.includes("T") ? v.replace(" ", "T") : v);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtDate(v: string | null | undefined, withTime = false): string {
  const d = toDate(v);
  if (!d) return "";
  return d.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    ...(withTime ? { hour: "numeric", minute: "2-digit" } : {}),
  });
}

export function relativeDue(v: string | null | undefined): string {
  const d = toDate(v);
  if (!d) return "No due date";
  const days = Math.round((d.getTime() - Date.now()) / 86_400_000);
  if (d.getTime() < Date.now()) return `Was due ${fmtDate(v)}`;
  if (days === 0) return "Due today";
  if (days === 1) return "Due tomorrow";
  return `Due ${fmtDate(v)}`;
}

export const SOURCE_LABEL: Record<string, string> = { SCHOOL: "School", PARENT: "Parent", SELF: "My own" };

export const STATUS_LABEL: Record<string, string> = {
  NOT_STARTED: "Not started",
  IN_PROGRESS: "In progress",
  COMPLETED: "Completed",
  SUBMITTED: "Submitted",
  SUBMITTED_LATE: "Submitted late",
  OVERDUE: "Overdue",
  DRAFT: "Draft",
  PROCESSING: "Preparing",
  PROCESSING_FAILED: "Needs attention",
  READY: "Ready for review",
  PUBLISHED: "Published",
  ACTIVE: "Active",
  CLOSED: "Closed",
  PREPARING: "Preparing",
  READY_TO_ASSIGN: "Ready to assign",
  FAILED: "Couldn't prepare",
  ASSIGNED: "Assigned",
  MASTERED: "Mastered",
  DEVELOPING: "Developing",
  NEEDS_REVIEW: "Needs review",
};

export function label(s: string | null | undefined): string {
  if (!s) return "";
  return STATUS_LABEL[s] || s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ");
}

export function contentTypeFor(file: File): string {
  const t = file.type;
  const n = file.name.toLowerCase();
  if (t === "application/pdf" || n.endsWith(".pdf")) return "PDF";
  if (t.startsWith("image/")) return "IMAGE";
  if (t.startsWith("video/")) return "VIDEO";
  if (t.startsWith("audio/")) return "AUDIO";
  if (n.endsWith(".docx") || n.endsWith(".doc")) return "DOCUMENT";
  return "OTHER";
}
