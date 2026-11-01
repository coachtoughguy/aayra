const env = import.meta.env;

export const config = {
  apiUrl: (env.VITE_API_URL || "http://localhost:8000").replace(/\/$/, ""),
  supabaseUrl: env.VITE_SUPABASE_URL || "",
  supabaseAnonKey: env.VITE_SUPABASE_ANON_KEY || "",
  studentLoginDomain: env.VITE_STUDENT_LOGIN_DOMAIN || "students.aayra.invalid",
  devLogin: env.VITE_DEV_LOGIN === "true",
};

export const supabaseConfigured = Boolean(config.supabaseUrl && config.supabaseAnonKey);
