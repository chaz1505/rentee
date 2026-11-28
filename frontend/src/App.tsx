import { useEffect, useMemo, useRef, useState, ReactNode } from "react";
import { Routes, Route, Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { ArrowRight, Building2, Check, ChevronRight, Clock3, Handshake, LayoutList, ListFilter, LoaderCircle, MessageCircle, Plus, Search, ShieldCheck, UserRound, Users } from "lucide-react";
import { agent, Inventory, Match, money, priceLabel } from "./data";
import { useStore } from "./store";
import { ConfirmDialog, Filters, Header, InboxZero, InventoryCard, MatchCard, OriginalMessage, Progress, ResetButton, Score, Shell, Status } from "./components";
import { api, ApiNetworkError, logout, redeem } from "./api";
const mine = (i: Inventory) => i.ownerId === agent.id;
const demo = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK_DATA === "true";
const days = (date: string) => Math.floor((Date.now() - new Date(date).getTime()) / 86400000);
function PageSentinel({resource}:{resource:"listings"|"leads"|"matches"}) {
  const {hasMore,states,loadNext}=useStore(), ref=useRef<HTMLDivElement>(null);
  useEffect(()=>{const node=ref.current;if(!node||!hasMore[resource])return;const observer=new IntersectionObserver(entries=>{if(entries[0].isIntersecting&&states[resource]!=="loading")loadNext(resource).catch(()=>{})},{rootMargin:"300px"});observer.observe(node);return()=>observer.disconnect()},[resource,hasMore,states,loadNext]);
  if(!hasMore[resource])return null;
  return <div ref={ref} className="grid min-h-14 place-items-center">{states[resource]==="error"?<button className="text-sm font-semibold text-brand" onClick={()=>loadNext(resource).catch(()=>{})}>Couldn’t load more. Retry</button>:<LoaderCircle className="animate-spin text-brand" size={20}/>}</div>
}
function Home() {
  const { inventory, matches, actionable, home, states, loadHome, profile, logoutLocal } = useStore();
  useEffect(()=>{loadHome().catch(()=>{})},[loadHome]);
  const pending=!home&&(states.home==="idle"||states.home==="loading");
  const own = inventory.filter(mine),
    listings = own.filter((i) => i.kind === "listing" && i.availability !== "unavailable"),
    leads = own.filter((i) => i.kind === "lead" && i.availability !== "unavailable"),
    newCount = home?.actionable_matches ?? matches.filter(actionable).length,
    needsL = home?.listings_needing_confirmation ?? listings.filter((i) => i.availability === "unconfirmed").length,
    needsQ = home?.leads_needing_confirmation ?? leads.filter((i) => i.availability === "unconfirmed").length;
  const ownMatches = matches.filter((m) => own.some((i) => i.id === m.listingId || i.id === m.leadId));
  const attention = [
    {
      n: newCount,
      label: "Unreviewed matches",
      to: "/matches",
      Icon: Handshake,
    },
    {
      n: needsL,
      label: "Listings to confirm",
      to: "/listings",
      Icon: Building2,
    },
    { n: needsQ, label: "Leads to confirm", to: "/leads", Icon: Users },
  ];
  return (
    <Shell>
      <header className="content flex items-center justify-between pb-5 pt-[max(24px,env(safe-area-inset-top))]">
        <div>
          <div className="text-2xl font-extrabold tracking-tight text-brand">rentee</div>
          <p className="mt-1 text-sm text-slate-500">Welcome, {profile?.name || home?.profile?.name || "Agent"}</p>
        </div>
        <div className="grid size-11 place-items-center rounded-full bg-pale text-brand">
          <UserRound size={21} />
        </div>
      </header>
      <div className="content space-y-5">
        <section className="rounded-3xl bg-[#123c31] p-5 text-white">
          <div className="flex items-center gap-2 text-emerald-200">
            <span className="size-2 rounded-full bg-emerald-300" />
            <span className="text-xs font-bold uppercase tracking-wider">Matching active</span>
          </div>
          <h1 className="mt-4 text-2xl font-bold leading-tight">Rentee is actively matching your listings and leads.</h1>
          {pending?<div className="skeleton mt-3 h-4 w-64 bg-white/15"/>:<p className="mt-3 text-sm text-emerald-100/80">{home.active_listings+home.active_leads} active inventory items are working across the network.</p>}
        </section>
        <section>
          <h2 className="mb-3 font-bold">Rentee network</h2>
          {pending?<StatsSkeleton/>:home?.network?<div className="card grid grid-cols-2 gap-y-5">{[[home.network.active_listings,"Active listings"],[home.network.active_leads,"Active leads"],[home.network.agents,"Agents"],[home.network.matches_this_week,"Matches this week"]].map(([n,l])=><div key={l}><b className="text-lg">{Number(n).toLocaleString()}</b><p className="text-xs text-slate-500">{l}</p></div>)}</div>:<LoadError onRetry={()=>loadHome(true)}/>}
        </section>
        <section>
          <h2 className="mb-3 font-bold">Your workspace</h2>
          {pending?<div className="grid grid-cols-2 gap-3"><StatCardsSkeleton/></div>:<div className="grid grid-cols-2 gap-3">
            {[
              [home?.active_listings ?? listings.length, "Active listings", "/listings"],
              [home?.active_leads ?? leads.length, "Active leads", "/leads"],
              [home?.matches_this_week ?? ownMatches.filter((m) => days(m.discovered.slice(0, 10)) <= 7).length, "Matches this week", "/matches?view=all"],
              [home?.lifetime_matches ?? ownMatches.length, "Lifetime matches", "/matches?view=all"],
            ].map(([n, l, to]) => (
              <Link key={String(l)} to={String(to)} className="card">
                <strong className="text-2xl">{n}</strong>
                <p className="mt-1 text-xs text-slate-500">{l}</p>
              </Link>
            ))}
          </div>}
        </section>
        <section>
          <div className="mb-3 flex items-center justify-between">
            <h2 className="font-bold">Needs your attention</h2>
            {newCount > 0 && <span className="text-xs font-semibold text-brand">{newCount + needsL + needsQ} items</span>}
          </div>
          {pending?<div className="card"><div className="skeleton h-16"/><div className="skeleton mt-3 h-16"/></div>:<div className="card divide-y divide-slate-100 p-0">
            {attention.map(({ n, label, to, Icon }) => (
              <Link key={label} to={to} className="flex min-h-16 items-center gap-3 px-4">
                <div className="grid size-9 place-items-center rounded-xl bg-pale text-brand">
                  <Icon size={18} />
                </div>
                <span className="flex-1 text-sm font-semibold">{label}</span>
                <b>{n}</b>
                <ChevronRight size={17} className="text-slate-400" />
              </Link>
            ))}
          </div>}
          {newCount > 0 && (
            <Link className="btn-primary mt-3 w-full" to="/matches">
              Review outstanding matches <ArrowRight size={17} />
            </Link>
          )}
        </section>
        <section>
          <h2 className="mb-3 font-bold">Recent activity</h2>
          <div className="space-y-3">
            {ownMatches
              .slice()
              .sort((a, b) => b.discovered.localeCompare(a.discovered))
              .slice(0, 3)
              .map((m) => {
                const own = inventory.find((i) => mine(i) && (i.id === m.listingId || i.id === m.leadId))!,
                  other = inventory.find((i) => !mine(i) && (i.id === m.listingId || i.id === m.leadId));
                return (
                  <Link to={`/match/${m.id}`} className="card flex gap-3" key={m.id}>
                    <div className="mt-1 grid size-9 shrink-0 place-items-center rounded-full bg-pale text-brand">
                      <Search size={17} />
                    </div>
                    <p className="text-sm leading-6">
                      <b>Rentee found {other?.kind === "lead" ? "a potential tenant" : "a property"}</b> for your {own.title}.
                      <span className="block text-xs text-slate-400">
                        {new Date(m.discovered).toLocaleDateString("en-MY", {
                          day: "numeric",
                          month: "short",
                        })}
                      </span>
                    </p>
                  </Link>
                );
              })}
          </div>
        </section>
        <button className="btn-secondary w-full" onClick={async()=>{await logout();logoutLocal();location.assign('/login')}}>Log out</button>
      </div>
    </Shell>
  );
}
function Matches() {
  const { inventory, matches, actionable, states, loadMatches } = useStore();
  useEffect(()=>{loadMatches().catch(()=>{})},[loadMatches]);
  const loc = useLocation(),
    nav = useNavigate(),
    params = new URLSearchParams(loc.search),
    scope = params.get("inventory");
  const [filter, setFilter] = useState(params.get("view") === "all" ? "All" : "New");
  const [grouped, setGrouped] = useState(true);
  const relevant = matches.filter((m) => inventory.some((i) => mine(i) && (i.id === m.listingId || i.id === m.leadId))).filter((m) => !scope || m.listingId === scope || m.leadId === scope);
  const shown = relevant.filter((m) => filter === "All" || (filter === "New" ? actionable(m) : m.status !== "new"));
  const ordered = [...shown].sort((a, b) => b.discovered.localeCompare(a.discovered));
  const newOnes = relevant.filter(actionable);
  const reviewed = relevant.filter((m) => m.status !== "new").length;
  const next = [...newOnes].sort((a, b) => b.score - a.score || b.discovered.localeCompare(a.discovered))[0];
  const groups = ordered.reduce<Record<string, Match[]>>((a, m) => {
    const own = inventory.find((i) => mine(i) && (i.id === m.listingId || i.id === m.leadId))!;
    (a[own.id] ??= []).push(m);
    return a;
  }, {});
  return (
    <Shell>
      <div className="content pb-5 pt-[max(24px,env(safe-area-inset-top))]">
        <p className="label">Opportunity inbox</p>
        <h1 className="mt-1 text-3xl font-extrabold">Matches</h1>
        <p className="mt-2 text-sm text-slate-500">
          {newOnes.length} awaiting review · {reviewed} reviewed
        </p>
        <div className="mt-5">
          <Progress done={reviewed} total={relevant.length} />
        </div>
        {next && (
          <button onClick={() => nav(`/match/${next.id}${scope ? `?scope=${scope}` : ""}`)} className="btn-primary mt-5 w-full">
            Review next match <ArrowRight size={17} />
          </button>
        )}
      </div>
      <div className="content space-y-4">
        <div className="flex items-center justify-between">
          <Filters items={["New", "Reviewed", "All"]} value={filter} onChange={setFilter} />
          <button className="grid size-10 place-items-center rounded-full border bg-white" onClick={() => setGrouped(!grouped)} aria-label="Toggle grouping">
            {grouped ? <LayoutList size={18} /> : <ListFilter size={18} />}
          </button>
        </div>
        {(states.matches==="idle"||states.matches==="loading")&&matches.length===0 ? <CardsSkeleton match/> : states.matches==="error"&&matches.length===0 ? <LoadError onRetry={()=>loadMatches(true)}/> : shown.length === 0 && filter === "New" ? (
          <InboxZero />
        ) : grouped ? (
          Object.entries(groups).map(([id, ms]) => {
            const own = inventory.find((i) => i.id === id)!;
            return (
              <section key={id}>
                <Link to={`/${own.kind}/${id}`} className="mb-2 flex items-end justify-between">
                  <div>
                    <p className="label">Your {own.kind}</p>
                    <h2 className="font-bold">{own.title}</h2>
                    <p className="text-xs text-slate-500">
                      {priceLabel(own)} · {own.bedrooms} bed
                    </p>
                  </div>
                  <span className="text-xs font-bold text-brand">{ms.filter(actionable).length} new</span>
                </Link>
                <div className="space-y-2">
                  {ms.map((m) => {
                    const other = inventory.find((i) => i.id === (m.listingId === id ? m.leadId : m.listingId))!;
                    return (
                      <Link key={m.id} to={`/match/${m.id}${scope ? `?scope=${scope}` : ""}`}>
                        <MatchCard match={m} other={other} />
                      </Link>
                    );
                  })}
                </div>
              </section>
            );
          })
        ) : (
          <div className="space-y-2">
            {ordered.map((m) => {
              const own = inventory.find((i) => mine(i) && (i.id === m.listingId || i.id === m.leadId))!,
                other = inventory.find((i) => i.id === (m.listingId === own.id ? m.leadId : m.listingId))!;
              return (
                <Link key={m.id} to={`/match/${m.id}`}>
                  <MatchCard match={m} other={other} />
                </Link>
              );
            })}
          </div>
        )}
        <PageSentinel resource="matches" />
      </div>
    </Shell>
  );
}
function InventoryList({ kind }: { kind: "listing" | "lead" }) {
  const { inventory, matches, actionable, confirm, deactivate, states, loadListings, loadLeads, loadMatches } = useStore();
  const loadKind=kind==="listing"?loadListings:loadLeads,state=kind==="listing"?states.listings:states.leads;
  useEffect(()=>{loadKind().catch(()=>{})},[loadKind]);
  useEffect(()=>{loadMatches().catch(()=>{})},[loadMatches]);
  const [filter, setFilter] = useState("Active"),
    [select, setSelect] = useState(false),
    [selected, setSelected] = useState<string[]>([]);
  const all = inventory.filter((i) => mine(i) && i.kind === kind),
    shown = all.filter((i) => (filter === "All" || filter === "Inactive" ? filter === "All" || i.availability === "unavailable" : filter === "Needs confirmation" ? i.availability === "unconfirmed" : i.availability !== "unavailable"));
  const counts = (id: string) => {
    const ms = matches.filter((m) => m.listingId === id || m.leadId === id);
    return { total: ms.length, new: ms.filter(actionable).length };
  };
  return (
    <Shell>
      <div className="content pb-4 pt-[max(24px,env(safe-area-inset-top))]">
        <p className="label">Your inventory</p>
        <div className="flex items-end justify-between">
          <h1 className="mt-1 text-3xl font-extrabold">My {kind === "listing" ? "listings" : "leads"}</h1>
          {demo&&<button
            className="text-sm font-semibold text-brand"
            onClick={() => {
              setSelect(!select);
              setSelected([]);
            }}
          >
            {select ? "Cancel" : "Select"}
          </button>}
        </div>
      </div>
      <div className="content space-y-4">
        {(state==="idle"||state==="loading")&&all.length===0?<CardsSkeleton/>:state==="error"&&all.length===0?<LoadError onRetry={()=>loadKind(true)}/>:null}
        <Filters items={["Active", "Needs confirmation", "Inactive", "All"]} value={filter} onChange={setFilter} />
        {demo && select && selected.length > 0 && (
          <button
            className="btn-primary w-full"
            onClick={() => {
              confirm(selected);
              setSelect(false);
              setSelected([]);
            }}
          >
            Confirm {selected.length} still active
          </button>
        )}
        {shown.map((i) => {
          const c = counts(i.id);
          return (
            <div key={i.id} className="relative">
              {demo && select && i.availability !== "unavailable" && (
                <button onClick={() => setSelected((s) => (s.includes(i.id) ? s.filter((x) => x !== i.id) : [...s, i.id]))} className={`absolute right-3 top-3 z-10 grid size-7 place-items-center rounded-full border ${selected.includes(i.id) ? "bg-brand text-white" : "bg-white"}`}>
                  {selected.includes(i.id) && <Check size={16} />}
                </button>
              )}
              <Link to={`/${kind}/${i.id}`}>
                <InventoryCard item={i} total={c.total} unreviewed={c.new} />
              </Link>
              <div className="-mt-2 flex gap-2 rounded-b-2xl border border-t-0 bg-white px-3 pb-3 pt-4">
                <Link to={`/matches?inventory=${i.id}`} className="btn-secondary flex-1">
                  View matches
                </Link>
                {demo && i.availability !== "unavailable" && (
                  <>
                    <button onClick={() => confirm([i.id])} className="btn-secondary px-3" aria-label="Confirm">
                      <ShieldCheck size={17} />
                    </button>
                    <ConfirmDialog title={`Deactivate ${i.title}?`} body="It will leave actionable review queues, while historical matches remain accessible." onConfirm={() => deactivate(i.id)}>
                      <button className="btn-secondary px-3 text-red-600" aria-label="Deactivate">
                        <Clock3 size={17} />
                      </button>
                    </ConfirmDialog>
                  </>
                )}
                {!demo&&<span className="self-center px-2 text-[11px] text-slate-400">Read-only pilot</span>}
              </div>
            </div>
          );
        })}
        <PageSentinel resource={kind === "listing" ? "listings" : "leads"} />
      </div>
    </Shell>
  );
}
function MatchDetail() {
  const { id } = useParams(),
    loc = useLocation(),
    nav = useNavigate();
  const { inventory, matches, actionable, loadMatchDetail } = useStore();
  const [detailState,setDetailState]=useState<"loading"|"ready"|"error">(matches.some(m=>m.id===id)?"ready":"loading");
  useEffect(()=>{if(!id)return;loadMatchDetail(id).then(()=>setDetailState("ready")).catch(()=>setDetailState("error"))},[id,loadMatchDetail]);
  const match = matches.find((m) => m.id === id);
  if (!match) return detailState==="error"?<DetailError onRetry={()=>{setDetailState("loading");id&&loadMatchDetail(id).then(()=>setDetailState("ready")).catch(()=>setDetailState("error"))}}/>:<DetailSkeleton title="Match details"/>;
  const listing = inventory.find((i) => i.id === match.listingId)!,
    lead = inventory.find((i) => i.id === match.leadId)!,
    own = mine(listing) ? listing : lead,
    other = mine(listing) ? lead : listing,
    scope = new URLSearchParams(loc.search).get("scope");
  const queue = matches.filter((m) => actionable(m) && (!scope || m.listingId === scope || m.leadId === scope)).sort((a, b) => b.score - a.score || b.discovered.localeCompare(a.discovered));
  const contact = async () => {
    if (import.meta.env.DEV && import.meta.env.VITE_USE_MOCK_DATA === "true") {
      alert(`Demo handoff: WhatsApp would open a chat with ${other.owner}. No message was sent.`);
      return;
    }
    try {
      const result = await api<{ phone: string }>(`/matches/${match.id}/contact`);
      window.location.assign(`https://wa.me/${result.phone.replace(/\D/g, "")}`);
    } catch {
      alert("This agent’s WhatsApp contact is currently unavailable.");
    }
  };
  const rows = [
    ["Development", listing.title, lead.developments.join(", ")],
    ["Location", listing.location, lead.location],
    ["Price / budget", priceLabel(listing), `${priceLabel(lead)} max`],
    ["Bedrooms", listing.bedrooms, lead.bedrooms],
    ["Property type", listing.propertyType, lead.propertyType],
    ["Furnishing", listing.furnishing || "Unknown", lead.furnishing || "Unknown"],
    ["Availability", listing.moveIn || "Unknown", lead.moveIn || "Unknown"],
  ];
  return (
    <Shell nav={false}>
      <Header title="Match details" sub={queue.length ? `${Math.max(1, queue.findIndex((m) => m.id === id) + 1)} of ${queue.length} in review queue` : undefined} />
      <div className="content space-y-5 pb-44 pt-5">
        <section className="card flex items-center gap-4">
          <Score score={match.score} />
          <div>
            <p className="label">
              Found{" "}
              {new Date(match.discovered).toLocaleDateString("en-MY", {
                day: "numeric",
                month: "long",
              })}
            </p>
            <h1 className="mt-1 text-xl font-extrabold">
              Potential {other.kind === "lead" ? "tenant" : "property"} for your {own.kind}
            </h1>
            {match.status !== "new" && <p className="mt-1 text-xs font-semibold capitalize text-brand">{match.status}</p>}
          </div>
        </section>
        {[
          [own, `Your ${own.kind}`],
          [other, `Matching ${other.kind}`],
        ].map(([x, label]) => {
          const item = x as Inventory;
          return (
            <section className="card" key={item.id}>
              <p className="label">{String(label)}</p>
              <div className="mt-2 flex justify-between gap-3">
                <div>
                  <h2 className="text-lg font-bold">{item.title}</h2>
                  <p className="text-sm text-slate-500">
                    {item.location} · {item.bedrooms} bed
                  </p>
                  <p className="mt-2 font-bold">
                    {priceLabel(item)}
                    {item.kind === "lead" ? " max" : ""}
                  </p>
                </div>
                <Link className="text-sm font-semibold text-brand" to={`/${item.kind}/${item.id}`}>
                  View
                </Link>
              </div>
              {!mine(item) && (
                <div className="mt-3 border-t pt-3 text-sm">
                  <b>{item.owner}</b>
                  <p className="text-slate-500">
                    {item.agency} · confirmed{" "}
                    {new Date(item.confirmed).toLocaleDateString("en-MY", {
                      day: "numeric",
                      month: "short",
                    })}
                  </p>
                </div>
              )}
            </section>
          );
        })}
        <section>
          <h2 className="mb-3 font-bold">At a glance</h2>
          <div className="card overflow-hidden p-0">
            <div className="grid grid-cols-[.8fr_1fr_1fr] bg-mist px-3 py-2 text-[11px] font-bold uppercase tracking-wide text-slate-500">
              <span>Criteria</span>
              <span>Listing</span>
              <span>Lead</span>
            </div>
            {rows.map(([label, a, b]) => (
              <div className="grid grid-cols-[.8fr_1fr_1fr] gap-2 border-t px-3 py-3 text-xs" key={label}>
                <b>{label}</b>
                <span className={a === "Unknown" ? "text-amber-700" : ""}>{a}</span>
                <span className={b === "Unknown" ? "text-amber-700" : ""}>{b}</span>
              </div>
            ))}
          </div>
          <p className="mt-2 text-xs text-slate-500">Unknown details are shown explicitly and are not treated as confirmed matches.</p>
        </section>
        <Explanation title="Why it matches" items={match.reasons} good />
        <Explanation title="Differences or compromises" items={match.differences.length ? match.differences : ["No material differences identified."]} />
        <Explanation title="Worth checking" items={match.checks} />
        <OriginalMessage message={other.message} label={`${other.owner}'s original message`} />
        <OriginalMessage message={own.message} label="Your original message" defaultOpen={false} />
      </div>
      <div className="safe-bottom fixed inset-x-0 bottom-0 z-30 mx-auto max-w-[760px] border-t bg-white/95 p-3 backdrop-blur">
        <button className="btn-primary w-full" onClick={contact}>
          <MessageCircle size={18} />
          Contact {other.owner} on WhatsApp
        </button>
        <p className="mt-2 text-center text-xs text-slate-500">Match review actions are disabled during the read-only pilot.</p>
      </div>
    </Shell>
  );
}
function Explanation({ title, items, good = false }: { title: string; items: string[]; good?: boolean }) {
  return (
    <section>
      <h2 className="mb-2 font-bold">{title}</h2>
      <div className="card space-y-2">
        {items.map((x) => (
          <div key={x} className="flex gap-2 text-sm leading-6">
            {good ? <Check size={16} className="mt-1 shrink-0 text-brand" /> : <span className="mt-2 size-1.5 shrink-0 rounded-full bg-slate-400" />}
            <span>{x}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
function InventoryDetail({ kind }: { kind: "listing" | "lead" }) {
  const { id } = useParams();
  const { inventory, matches, actionable, confirm, deactivate, loadInventoryDetail, loadMatches } = useStore();
  const [detailState,setDetailState]=useState<"loading"|"ready"|"error">(inventory.some(i=>i.id===id&&i.kind===kind)?"ready":"loading");
  useEffect(()=>{if(!id)return;loadInventoryDetail(kind,id).then(()=>setDetailState("ready")).catch(()=>setDetailState("error"))},[id,kind,loadInventoryDetail]);
  useEffect(()=>{loadMatches().catch(()=>{})},[loadMatches]);
  const item = inventory.find((i) => i.id === id && i.kind === kind);
  if (!item) return detailState==="error"?<DetailError onRetry={()=>{setDetailState("loading");id&&loadInventoryDetail(kind,id).then(()=>setDetailState("ready")).catch(()=>setDetailState("error"))}}/>:<DetailSkeleton title={`${kind === "listing" ? "Listing" : "Lead"} details`}/>;
  const owner = mine(item),
    ms = matches.filter((m) => m.listingId === id || m.leadId === id),
    newCount = ms.filter(actionable).length;
  const facts =
    kind === "listing"
      ? [
          [item.intent, priceLabel(item)],
          ["Bedrooms", item.bedrooms],
          ["Bathrooms", item.bathrooms || "Not provided"],
          ["Size", item.size || "Not provided"],
          ["Property type", item.propertyType],
          ["Furnishing", item.furnishing || "Not provided"],
          ["Availability", item.moveIn || "Not provided"],
        ]
      : [
          [item.intent, `${priceLabel(item)} max`],
          ["Bedrooms", item.bedrooms],
          ["Preferred areas", item.location],
          ["Developments", item.developments.join(", ")],
          ["Property type", item.propertyType],
          ["Furnishing", item.furnishing || "Not provided"],
          ["Move-in", item.moveIn || "Not provided"],
        ];
  return (
    <Shell>
      <Header title={`${kind === "listing" ? "Listing" : "Lead"} details`} />
      <div className="content space-y-5 pb-5 pt-5">
        <section>
          <div className="flex items-center gap-2">
            <Status value={item.availability} freshness={item.freshness} />
          </div>
          <h1 className="mt-3 text-3xl font-extrabold">{item.title}</h1>
          <p className="mt-1 text-slate-500">{item.location}</p>
        </section>
        <section className="card grid grid-cols-2 gap-x-4 gap-y-5">
          {facts.map(([l, v]) => (
            <div key={l}>
              <p className="label">{l}</p>
              <p className="mt-1 text-sm font-semibold">{v}</p>
            </div>
          ))}
        </section>
        <section className="card">
          <p className="label">{kind === "listing" ? "Listing" : "Lead"} agent</p>
          <h2 className="mt-2 font-bold">{item.owner}</h2>
          <p className="text-sm text-slate-500">{item.agency}</p>
          <div className="mt-3 border-t pt-3 text-xs text-slate-500">
            Posted{" "}
            {new Date(item.posted).toLocaleDateString("en-MY", {
              day: "numeric",
              month: "long",
              year: "numeric",
            })}{" "}
            {item.confirmed&&<>· Last confirmed{" "}
            {new Date(item.confirmed).toLocaleDateString("en-MY", {
              day: "numeric",
              month: "short",
            })}</>}
          </div>
        </section>
        <OriginalMessage message={item.message} label={`Original WhatsApp ${kind}`} />
        {owner ? (
          <section className="card">
            <p className="label">Owner controls</p>
            <div className="mt-2 flex items-end justify-between">
              <div>
                <b className="text-2xl">{ms.length}</b>
                <p className="text-sm text-slate-500">Total qualified matches · {newCount} unreviewed</p>
              </div>
            </div>
            <div className={`mt-4 grid ${demo?'grid-cols-2':'grid-cols-1'} gap-2`}>
              <Link className="btn-primary" to={`/matches?inventory=${id}`}>
                View matches
              </Link>
              {demo&&<button className="btn-secondary" onClick={() => confirm([item.id])}>
                Confirm active
              </button>}
            </div>
            {demo&&item.availability !== "unavailable" && (
              <ConfirmDialog title={`Deactivate ${item.title}?`} body="Historical matches will be preserved." onConfirm={() => deactivate(item.id)}>
                <button className="mt-2 w-full text-center text-sm font-semibold text-red-600">Deactivate</button>
              </ConfirmDialog>
            )}
            {!demo&&<p className="mt-3 text-[11px] text-slate-400">Inventory changes are disabled during the read-only pilot.</p>}
          </section>
        ) : (
          <button onClick={() => alert(`Demo handoff: WhatsApp would open a chat with ${item.owner}. No message was sent.`)} className="btn-primary w-full">
            <MessageCircle size={18} />
            Contact agent on WhatsApp
          </button>
        )}
        <Link to={kind === "listing" ? "/listings" : "/leads"} className="block text-center text-sm font-semibold text-brand">
          Back to my workspace
        </Link>
      </div>
    </Shell>
  );
}
function StatsSkeleton(){return <div className="card grid grid-cols-2 gap-5">{Array.from({length:4},(_,i)=><div key={i}><div className="skeleton h-6 w-16"/><div className="skeleton mt-2 h-3 w-24"/></div>)}</div>}
function StatCardsSkeleton(){return <>{Array.from({length:4},(_,i)=><div className="card" key={i}><div className="skeleton h-8 w-14"/><div className="skeleton mt-2 h-3 w-24"/></div>)}</>}
function CardsSkeleton({match=false}:{match?:boolean}){return <div className="space-y-3" aria-label="Loading content">{Array.from({length:3},(_,i)=><div className="card" key={i}><div className="flex gap-3">{match&&<div className="skeleton size-14 rounded-full"/>}<div className="flex-1"><div className="skeleton h-4 w-2/3"/><div className="skeleton mt-3 h-3 w-1/2"/><div className="skeleton mt-4 h-3 w-full"/></div></div></div>)}</div>}
function LoadError({onRetry}:{onRetry:()=>void}){return <div className="card text-center"><p className="text-sm text-slate-600">We couldn't load this section.</p><button className="btn-secondary mt-3" onClick={onRetry}>Try again</button></div>}
function DetailSkeleton({title}:{title:string}){return <Shell nav={false}><Header title={title}/><div className="content pt-5"><CardsSkeleton/></div></Shell>}
function DetailError({onRetry}:{onRetry:()=>void}){return <Shell nav={false}><Header title="Unable to load"/><div className="content py-10"><LoadError onRetry={onRetry}/></div></Shell>}
function NotFound() {
  return (
    <Shell nav={false}>
      <Header title="Not found" />
      <div className="content py-20 text-center">
        <h1 className="text-2xl font-bold">This item isn't available</h1>
        <Link to="/" className="btn-primary mt-5">
          Back home
        </Link>
      </div>
    </Shell>
  );
}
function Login() {
  const nav = useNavigate(),
    [token, setToken] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [slow,setSlow]=useState(false);
  useEffect(()=>{if(!busy){setSlow(false);return}const timer=window.setTimeout(()=>setSlow(true),5000);return()=>window.clearTimeout(timer)},[busy]);
  useEffect(() => {
    const params = new URLSearchParams(location.hash.slice(1));
    setToken(params.get("token") || "");
    if (location.pathname !== "/login") sessionStorage.setItem("rentee_return_path", location.pathname + location.search);
    history.replaceState(null, "", location.pathname + location.search);
  }, []);
  const open = async () => {
    if (!token) return;
    setBusy(true);
    setError("");
    try {
      await redeem(token);
      setToken("");
      const candidate=sessionStorage.getItem("rentee_return_path") || "/";
      const destination=/^\/(?:matches?\/[A-Za-z0-9_-]+|matches|listings|leads|listing\/[A-Za-z0-9_-]+|lead\/[A-Za-z0-9_-]+)(?:\?[^#]*)?$/.test(candidate)?candidate:"/";
      sessionStorage.removeItem("rentee_return_path");
      nav(destination);
      location.reload();
    } catch (e) {
      setError(e instanceof ApiNetworkError ? e.message : "This login link is invalid, expired, or already used.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <main className="page grid min-h-screen place-items-center px-4">
      <section className="card w-full max-w-sm p-6">
        <div className="text-2xl font-extrabold text-brand">rentee</div>
        <h1 className="mt-5 text-2xl font-extrabold">Your agent workspace</h1>
        <p className="mt-3 text-sm leading-6 text-slate-600">
          Send <b>/app</b> to Rentee on WhatsApp to receive a secure login link.
        </p>
        {token && (
          <button disabled={busy} onClick={open} className="btn-primary mt-5 w-full">
            {busy ? <><LoaderCircle className="animate-spin" size={18}/>Opening your workspace...</> : "Open my workspace"}
          </button>
        )}
        {slow&&<p className="mt-3 text-center text-xs text-slate-500">Still loading your workspace...</p>}
        {error && <p className="mt-3 text-sm text-red-700">{error}</p>}
      </section>
    </main>
  );
}
function Gate({ children }: { children: ReactNode }) {
  const { authLoading, authenticated, error, reload } = useStore();
  if (location.pathname === "/login") return <>{children}</>;
  if (authLoading)
    return (
      <main className="page grid min-h-screen place-items-center">
        <div className="text-center"><LoaderCircle className="mx-auto animate-spin text-brand"/><p className="mt-3 text-sm text-slate-600">Opening your workspace...</p></div>
      </main>
    );
  if (authenticated && new URLSearchParams(location.hash.slice(1)).has("token")) history.replaceState(null,"",location.pathname+location.search);
  if (!authenticated && new URLSearchParams(location.hash.slice(1)).has("token")) return <Login/>;
  if (!authenticated)
    return (
      <main className="page grid min-h-screen place-items-center px-4">
        <section className="card text-center">
          <h1 className="text-xl font-bold">{error === "session" ? "Your session has expired" : "Workspace unavailable"}</h1>
          <Link className="btn-primary mt-4" to="/login">
            Log in securely
          </Link>
          <button className="btn-secondary mt-2 w-full" onClick={reload}>
            Try again
          </button>
        </section>
      </main>
    );
  return <>{children}</>;
}
export default function App() {
  return (
    <Gate>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<Home />} />
        <Route path="/matches" element={<Matches />} />
        <Route path="/match/:id" element={<MatchDetail />} />
        <Route path="/matches/:id" element={<MatchDetail />} />
        <Route path="/listings" element={<InventoryList kind="listing" />} />
        <Route path="/leads" element={<InventoryList kind="lead" />} />
        <Route path="/listing/:id" element={<InventoryDetail kind="listing" />} />
        <Route path="/lead/:id" element={<InventoryDetail kind="lead" />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </Gate>
  );
}
