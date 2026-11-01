import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import { config, supabaseConfigured } from "./config";

// Captured before supabase-js consumes the URL: invite and password-recovery links must lead to
// "set your password".
export const arrivedViaLink: "invite" | "recovery" | null = (() => {
  const h = typeof window !== "undefined" ? window.location.hash + window.location.search : "";
  if (h.includes("type=invite")) return "invite";
  if (h.includes("type=recovery")) return "recovery";
  return null;
})();

export const supabase: SupabaseClient | null = supabaseConfigured
  ? createClient(config.supabaseUrl, config.supabaseAnonKey, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    })
  : null;

export function loginEmail(identifier: string): string {
  const v = identifier.trim();
  return v.includes("@") ? v : `${v.toLowerCase()}@${config.studentLoginDomain}`;
}
