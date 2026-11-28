import { createContext, useContext, useEffect, useMemo, useState, ReactNode } from "react";
import { inventory as seedInventory, matches as seedMatches, Inventory, Match, MatchStatus } from "./data";
import { api, ApiError } from "./api";
type Store = {
  inventory: Inventory[];
  matches: Match[];
  loading: boolean;
  error: string;
  authenticated: boolean;
  home: any | null;
  reload: () => void;
  setMatch: (id: string, status: MatchStatus) => Promise<void>;
  confirm: (ids: string[]) => void;
  deactivate: (id: string) => void;
  reset: () => void;
  actionable: (m: Match) => boolean;
};
const C = createContext<Store | null>(null),
  KEY = "rentee-demo-v1",
  mock = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK_DATA === "true";
const load = () => {
  try {
    return JSON.parse(localStorage.getItem(KEY) || "null");
  } catch {
    return null;
  }
};
const inv = (x: any, me: string): Inventory => ({
  id: x.id,
  kind: x.kind,
  ownerId: x.owner?.id === me ? "a1" : "other",
  owner: x.owner?.name || "Rentee agent",
  agency: x.owner?.agency || "",
  intent: x.transaction_type || "",
  title: x.title || x.kind,
  location: x.location || "",
  developments: x.title ? [x.title] : [],
  price: Number(x.price || 0),
  bedrooms: String(x.bedrooms || "Unknown"),
  bathrooms: x.bathrooms ? String(x.bathrooms) : undefined,
  size: x.size ? `${Number(x.size).toLocaleString()} sq ft` : undefined,
  propertyType: x.property_type || "Unknown",
  furnishing: x.furnishing || undefined,
  moveIn: x.availability || undefined,
  posted: x.created_at || "",
  confirmed: x.last_confirmed_active || x.created_at || "",
  availability: x.active ? (x.freshness === "needs_confirmation" ? "unconfirmed" : "available") : "unavailable",
  message: x.original_message || "",
});
export function StoreProvider({ children }: { children: ReactNode }) {
  const cached = mock ? load() : null,
    [inventory, setInventory] = useState<Inventory[]>(cached?.inventory || (mock ? seedInventory : [])),
    [matches, setMatches] = useState<Match[]>(cached?.matches || (mock ? seedMatches : [])),
    [loading, setLoading] = useState(!mock),
    [error, setError] = useState(""),
    [authenticated, setAuthenticated] = useState(mock);
  const [home, setHome] = useState<any | null>(null);
  const save = (i: Inventory[], m: Match[]) => {
    if (mock) localStorage.setItem(KEY, JSON.stringify({ inventory: i, matches: m }));
  };
  const reload = () => {
    if (mock) return;
    setLoading(true);
    setError("");
    Promise.all([api<any>("/me"), api<any>("/listings?limit=100"), api<any>("/leads?limit=100"), api<any>("/matches"),api<any>("/home")])
      .then(([me, ls, qs, ms,homeData]) => {
        const byId = new Map<string, Inventory>();
        [...ls.items, ...qs.items, ...ms.items.flatMap((m: any) => [m.listing, m.lead])].filter(Boolean).forEach((x: any) => byId.set(x.id, inv(x, me.id)));
        setInventory([...byId.values()]);
        setMatches(
          ms.items.map((m: any) => ({
            id: m.id,
            listingId: m.listing_id,
            leadId: m.lead_id,
            score: m.score,
            discovered: m.created_at,
            reasons: Array.isArray(m.reason) ? m.reason : [m.reason].filter(Boolean),
            differences: [],
            checks: [],
            status: m.review_status || "new",
          })),
        );
        setHome(homeData);
        setAuthenticated(true);
      })
      .catch((e) => {
        setAuthenticated(false);
        setInventory([]);
        setMatches([]);
        setError(e instanceof ApiError && e.status === 401 ? "session" : "api");
      })
      .finally(() => setLoading(false));
  };
  useEffect(reload, []);
  const setMatch = async (id: string, status: MatchStatus) => {
    if (mock) {
      setMatches((old) => {
        const next = old.map((x) => (x.id === id ? { ...x, status } : x));
        save(inventory, next);
        return next;
      });
      return;
    }
    // The restricted pilot is read-only. Match state is mutable only in the
    // explicitly enabled local demo, never against production Bubble data.
    throw new Error(`Match mutation is disabled (${id}, ${status})`);
  };
  const confirm = (ids: string[]) => {
    if (mock)
      setInventory((old) => {
        const next = old.map((x) =>
          ids.includes(x.id)
            ? {
                ...x,
                availability: "available" as const,
                confirmed: new Date().toISOString().slice(0, 10),
              }
            : x,
        );
        save(next, matches);
        return next;
      });
  };
  const deactivate = (id: string) => {
    if (mock)
      setInventory((old) => {
        const next = old.map((x) => (x.id === id ? { ...x, availability: "unavailable" as const } : x));
        save(next, matches);
        return next;
      });
  };
  const reset = () => {
    if (mock) {
      setInventory(seedInventory);
      setMatches(seedMatches);
      localStorage.removeItem(KEY);
    }
  };
  const actionable = (m: Match) => m.status === "new" && inventory.some((i) => (i.id === m.listingId || i.id === m.leadId) && i.ownerId === "a1" && i.availability !== "unavailable") && inventory.filter((i) => i.id === m.listingId || i.id === m.leadId).every((i) => i.availability !== "unavailable");
  return (
    <C.Provider
      value={useMemo(
        () => ({
          inventory,
          matches,
          loading,
          error,
          authenticated,
          home,
          reload,
          setMatch,
          confirm,
          deactivate,
          reset,
          actionable,
        }),
        [inventory, matches, loading, error, authenticated, home],
      )}
    >
      {children}
    </C.Provider>
  );
}
export const useStore = () => {
  const x = useContext(C);
  if (!x) throw Error("Missing StoreProvider");
  return x;
};
