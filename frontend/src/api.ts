export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  skill_id: string;
  updated_at?: string;
  last_message_preview?: string;
  last_message_at?: string;
}
export interface Message {
  id: string;
  speaker: "USER" | "AGENT";
  text: string;
  created_at: string;
  evidence_id?: string;
  sequence?: number;
  client_request_id?: string;
  deleted?: boolean;
  reply_state?: "WAITING" | "REPLIED" | "FAILED";
  retry_payload?: { text: string; domain: string; remember: boolean } | null;
}
export interface Evidence {
  id: string;
  text: string;
  origin: string;
  claim_kind: string;
  domain: string;
  created_at: string;
  status: string;
}
export interface Trait {
  id: string;
  proposition: string;
  status: string;
  supports: string[];
  counters: string[];
  domain: string;
}
export interface Directive {
  id: string;
  objective: string;
  domain: string;
  limits: string;
  state: string;
  revision: number;
}
export interface Version {
  id: string;
  track: string;
  state: string;
  created_at: string;
  evidence_ids: string[];
}
export interface Skill {
  id: string;
  display_name: string;
  description: string;
  status: string;
  builtin?: boolean;
  slug?: string;
  domain?: string;
  tags?: string[];
  avatar_url?: string;
  starter_prompts?: string[];
  source?: string;
  source_version?: string;
  license?: string;
  rank?: number;
}
export interface Turn {
  id: string;
  speaker: string;
  text: string;
  turn_index: number;
  is_generated: boolean;
}
export interface Simulation {
  id: string;
  topic: string;
  state: string;
  persona_version_id: string;
  skill_id: string;
  max_turns: number;
  used_tokens?: number;
  max_tokens?: number;
  failure?: string;
  turns: Turn[];
  feedback: { turn_id: string; verdict: string; comment?: string }[];
}
export interface Growth {
  id: string;
  title: string;
  trigger: string;
  action: string;
  state: string;
  outcome?: string;
}
export interface AppState {
  conversations: Conversation[];
  evidence: Evidence[];
  mirror: { PERCEIVED: Trait[]; OBSERVED: Trait[]; CHOSEN: Directive[] };
  directives: Directive[];
  versions: Version[];
  skills: Skill[];
  simulations: Simulation[];
  growth: Growth[];
  privacy: { provider: string; external_calls: number; telemetry: boolean };
  stage: string;
  upgrade?: UpgradeState;
}

export interface ModelProvider { id: string; name?: string; label?: string; protocol?: string; base_url?: string; models?: string[]; regions?: string[]; fields?: string[]; }
export interface ModelConnection { id: string; label?: string; provider: string; model: string; protocol?: string; base_url?: string; region?: string; workspace_id?: string; is_default?: boolean; default?: boolean; configured?: boolean; key_status?:string;api_key_mask?:string;storage?:string; credential_status?: string; key_mask?: string; remember?: boolean; last_test?: {status?: string; success?: boolean; message?: string}; }
export interface UserSkillVersion { id: string; text?: string; content_hash?: string; created_at?: string; evidence_ids?: string[]; state?: string;status?:string; }
export interface MeProfile { nickname: string; bio: string; avatar_url?: string; self_statements?: (Evidence|string)[]; mirror?: AppState["mirror"]; goals?: Directive[]; versions?: UserSkillVersion[]; draft?: UserSkillVersion|null; certified?: UserSkillVersion|null; assessments?: AssessmentTask[]; }
export interface AssessmentDimension { id: string; label: string; score: number; threshold: number; issues?: (string|{excerpt?:string;issue?:string;reason?:string;question_id?:string})[]; suggestions?: string[]; }
export interface AssessmentQuestion { id: string; domain?: string; prompt: string; options?: Record<string,string>; }
export interface AssessmentReview { question_id: string; required?: boolean; disputed?: boolean; reference?: {choice?:string;reason?:string}; candidates?: {candidate_id: string;choice?:string;reason?:string;scores?:Record<string,number>}[]; review?: {accepted:boolean;comment?:string}; }
export interface AssessmentTask { id: string; kind: string; status: string; subject_id?:string; stage?:string; created_at?:string; progress?: {completed:number;total:number;percent:number}; questions?:AssessmentQuestion[]; answer_status?:string[]; reviews?:AssessmentReview[]; report?: {name?:string;title?:string;overall?:number;passed?:boolean;dimensions?:AssessmentDimension[]; reasons?:string[]; safety_checks?:Record<string,boolean>; critical_checks?:Record<string,boolean>;model?:string|{provider?:string;model?:string}; protocol?:string; sample_count?:number; baselines?:{id:string;label:string;overall:number;dimensions?:Record<string,number>}[];[key:string]:unknown}; failure?:string|{code?:string;message?:string}; error?:string; }
export interface SkillImport { id:string;text:string;display_name?:string;description?:string;source:string;permission:string;category?:string;version?:number;status:string;assessment_id?:string;quality_report?:AssessmentTask["report"]; }
export interface VirtualParticipant { id:string;version_id:string;kind:"CHARACTER"|"SELF";display_name:string;avatar_url?:string; }
export interface VirtualTurn { id:string;speaker_id:string;display_name:string;avatar_url?:string;text:string;turn_index:number;is_generated:true;version_id?:string; }
export interface VirtualSession {id:string;topic:string;state:string;participants:VirtualParticipant[];turns:VirtualTurn[];draft?:{id:string;speaker_id:string;text:string;publish_at?:string}|null;used_tokens:number;max_turns:number;max_tokens:number;next_at?:string;failure?:string;typing?:boolean;revision?:number;interventions?:{id:string;scope:string;source_id?:string;participant_version?:string;human_confirmed:boolean;text:string;created_at?:string}[];}
export interface UpgradeState {connections:ModelConnection[];providers:ModelProvider[];me:MeProfile;unlock:{unlocked:boolean;grant?:{profile_version_id?:string};reason?:string};preferences:{setup_dismissed?:boolean};skill_imports:SkillImport[];virtual_sessions:VirtualSession[];}

let token = "";
export async function connect() {
  const session = await request<{ token: string }>("/session");
  token = session.token;
  return request<AppState>("/state");
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: "same-origin",
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-WOT-Token": token } : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    let detail = "请求没有完成，请稍后重试。";
    try {
      const error = await response.json();
      if (typeof error.detail === "string") detail = error.detail;
      else if (Array.isArray(error.detail)) detail = "请检查填写的内容后重试。";
    } catch {
      /* A non-JSON upstream error should keep the readable fallback. */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export function post<T>(
  path: string,
  body: unknown = {},
  idempotencyKey: string = crypto.randomUUID(),
): Promise<T> {
  return request<T>(path, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": idempotencyKey },
  });
}
