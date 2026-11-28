import {createContext,useContext,useMemo,useState,ReactNode} from 'react';
import {inventory as seedInventory,matches as seedMatches,Inventory,Match,MatchStatus} from './data';
type Store={inventory:Inventory[];matches:Match[];setMatch:(id:string,status:MatchStatus)=>void;confirm:(ids:string[])=>void;deactivate:(id:string)=>void;reset:()=>void;actionable:(m:Match)=>boolean};
const C=createContext<Store|null>(null); const KEY='rentee-demo-v1';
const load=()=>{try{return JSON.parse(localStorage.getItem(KEY)||'null')}catch{return null}};
export function StoreProvider({children}:{children:ReactNode}){const cached=load();const [inventory,setInventory]=useState<Inventory[]>(cached?.inventory||seedInventory);const [matches,setMatches]=useState<Match[]>(cached?.matches||seedMatches);const save=(i:Inventory[],m:Match[])=>localStorage.setItem(KEY,JSON.stringify({inventory:i,matches:m}));
 const setMatch=(id:string,status:MatchStatus)=>setMatches(old=>{const next=old.map(x=>x.id===id?{...x,status}:x);save(inventory,next);return next});
 const confirm=(ids:string[])=>setInventory(old=>{const next=old.map(x=>ids.includes(x.id)?{...x,availability:'available' as const,confirmed:new Date().toISOString().slice(0,10)}:x);save(next,matches);return next});
 const deactivate=(id:string)=>setInventory(old=>{const next=old.map(x=>x.id===id?{...x,availability:'unavailable' as const}:x);save(next,matches);return next});
 const reset=()=>{setInventory(seedInventory);setMatches(seedMatches);localStorage.removeItem(KEY)};
 const actionable=(m:Match)=>m.status==='new'&&inventory.some(i=>(i.id===m.listingId||i.id===m.leadId)&&i.ownerId==='a1'&&i.availability!=='unavailable')&&inventory.filter(i=>i.id===m.listingId||i.id===m.leadId).every(i=>i.availability!=='unavailable');
 return <C.Provider value={useMemo(()=>({inventory,matches,setMatch,confirm,deactivate,reset,actionable}),[inventory,matches])}>{children}</C.Provider>}
export const useStore=()=>{const x=useContext(C);if(!x)throw Error('Missing StoreProvider');return x};
