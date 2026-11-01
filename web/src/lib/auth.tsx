import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { ApiError, get, setTokenGetter, setUnauthorizedHandler } from "./api";
import { config } from "./config";
import { readLocal, writeLocal } from "./storage";
import { arrivedViaLink, loginEmail, supabase } from "./supabase";

export interface Me {
  user_id: string;
  display_name: string;
  email: string | null;
  roles: Record<string, string[]>;
  schools: { school_id: string; name: string; school_kind: string; timezone: string; roles: string[] }[];
  student: null | {
    student_id: string;
    school_id: string;
    school_kind: string;
    school_name: string;
    grade_name: string;
    section_name: string;
  };
  homes: ("STUDENT" | "INDEPENDENT" | "TEACHER" | "PARENT" | "ADMIN")[];
}

type Status = "loading" | "signedOut" | "noAccount" | "setPassword" | "ready";

interface AuthState {
  status: Status;
  me: Me | null;
  email: string | null;
  signIn: (identifier: string, password: string) => Promise<void>;
  signUp: (email: string, password: string) => Promise<{ needsConfirmation: boolean }>;
  devSignIn: (token: string) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
  setPassword: (password: string) => Promise<void>;
  sendReset: (email: string) => Promise<void>;
}

const Ctx = createContext<AuthState | null>(null);
const DEV_KEY = "aayra.devToken";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<Status>("loading");
  const [me, setMe] = useState<Me | null>(null);
  const [email, setEmail] = useState<string | null>(null);
  const [mustSetPassword, setMustSetPassword] = useState<boolean>(arrivedViaLink !== null);

  useEffect(() => {
    setTokenGetter(async () => {
      if (config.devLogin) return readLocal(DEV_KEY);
      if (!supabase) return null;
      const { data } = await supabase.auth.getSession();
      return data.session?.access_token ?? null;
    });
  }, []);

  const loadMe = useCallback(async () => {
    try {
      const m = await get<Me>("/v1/me");
      setMe(m);
      setStatus(mustSetPassword ? "setPassword" : "ready");
    } catch (e) {
      setMe(null);
      if (e instanceof ApiError && e.code === "NO_ACCOUNT") setStatus(mustSetPassword ? "setPassword" : "noAccount");
      else if (e instanceof ApiError && e.status === 401) setStatus("signedOut");
      else setStatus("signedOut");
    }
  }, [mustSetPassword]);

  useEffect(() => {
    setUnauthorizedHandler((code) => {
      if (code === "UNAUTHENTICATED") setStatus("signedOut");
    });
  }, []);

  useEffect(() => {
    if (config.devLogin) {
      if (readLocal(DEV_KEY)) loadMe();
      else setStatus("signedOut");
      return;
    }
    if (!supabase) {
      setStatus("signedOut");
      return;
    }
    const sync = async () => {
      const { data } = await supabase!.auth.getSession();
      const s = data.session;
      if (!s) {
        setStatus("signedOut");
        return;
      }
      setEmail(s.user.email ?? null);
      if (s.user.user_metadata?.must_change_password) setMustSetPassword(true);
      await loadMe();
    };
    sync();
    const { data: sub } = supabase.auth.onAuthStateChange((event, session) => {
      if (event === "PASSWORD_RECOVERY") setMustSetPassword(true);
      if (event === "SIGNED_OUT" || !session) setStatus("signedOut");
      if (event === "SIGNED_IN" || event === "TOKEN_REFRESHED" || event === "USER_UPDATED") {
        setEmail(session?.user.email ?? null);
        if (session?.user.user_metadata?.must_change_password) setMustSetPassword(true);
      }
    });
    return () => sub.subscription.unsubscribe();
  }, [loadMe]);

  useEffect(() => {
    if (mustSetPassword && (status === "ready" || status === "noAccount")) setStatus("setPassword");
  }, [mustSetPassword, status]);

  const value = useMemo<AuthState>(
    () => ({
      status,
      me,
      email,
      async signIn(identifier, password) {
        if (!supabase) throw new Error("Sign-in isn't configured for this site.");
        const { data, error } = await supabase.auth.signInWithPassword({ email: loginEmail(identifier), password });
        if (error) throw new Error(error.message === "Invalid login credentials" ? "That username/email and password don't match." : error.message);
        setEmail(data.user?.email ?? null);
        if (data.user?.user_metadata?.must_change_password) setMustSetPassword(true);
        await loadMe();
      },
      async signUp(addr, password) {
        if (!supabase) throw new Error("Sign-up isn't configured for this site.");
        const { data, error } = await supabase.auth.signUp({
          email: addr.trim(),
          password,
          options: { emailRedirectTo: window.location.origin + "/welcome" },
        });
        if (error) throw new Error(error.message);
        if (data.session) {
          setEmail(addr.trim());
          await loadMe();
          return { needsConfirmation: false };
        }
        return { needsConfirmation: true };
      },
      async devSignIn(token) {
        writeLocal(DEV_KEY, token);
        setStatus("loading");
        await loadMe();
      },
      async signOut() {
        if (config.devLogin) writeLocal(DEV_KEY, null);
        else await supabase?.auth.signOut();
        setMe(null);
        setStatus("signedOut");
      },
      refresh: loadMe,
      async setPassword(password) {
        if (!supabase) throw new Error("Not available.");
        const { error } = await supabase.auth.updateUser({ password, data: { must_change_password: false } });
        if (error) throw new Error(error.message);
        setMustSetPassword(false);
        if (window.location.hash) history.replaceState(null, "", window.location.pathname);
        setStatus("loading");
        try {
          const m = await get<Me>("/v1/me");
          setMe(m);
          setStatus("ready");
        } catch (e) {
          setStatus(e instanceof ApiError && e.code === "NO_ACCOUNT" ? "noAccount" : "signedOut");
        }
      },
      async sendReset(addr) {
        if (!supabase) throw new Error("Not available.");
        const { error } = await supabase.auth.resetPasswordForEmail(addr.trim(), {
          redirectTo: window.location.origin + "/set-password",
        });
        if (error) throw new Error(error.message);
      },
    }),
    [status, me, email, loadMe],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
