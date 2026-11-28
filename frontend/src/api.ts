export const API_BASE=(import.meta.env.VITE_AGENT_API_BASE_URL||'').replace(/\/$/,'');
export class ApiError extends Error{constructor(message:string,public status:number){super(message)}}
export class ApiNetworkError extends Error{constructor(){super('Unable to reach Rentee. Check your connection and try again.')}}
export async function api<T>(path:string,init:RequestInit={}):Promise<T>{
  const headers=new Headers(init.headers);
  if(init.body!=null&&!headers.has('Content-Type'))headers.set('Content-Type','application/json');
  let response:Response;
  try{response=await fetch(`${API_BASE}/api/agent${path}`,{...init,credentials:'include',headers})}
  catch{throw new ApiNetworkError()}
  let body:any={};try{body=await response.json()}catch{}
  if(!response.ok)throw new ApiError(body.error||'Something went wrong',response.status);
  return body as T
}
export const redeem=(token:string)=>api<{ok:boolean}>('/auth/redeem',{method:'POST',body:JSON.stringify({token})});
export const logout=()=>api<{ok:boolean}>('/auth/logout',{method:'POST'});
