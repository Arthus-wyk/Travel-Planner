const base = (import.meta.env?.VITE_API_BASE || "http://127.0.0.1:8788").replace(/\/$/, "");
export const apiUrl = (path) => `${base}${path}`;
