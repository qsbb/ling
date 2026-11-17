export interface DetailAccount {
  account_id: string;
  subject_id: string;
  connector_instance: string;
  external_account_id: string;
  proof: string;
  revoked: boolean;
}

export function accountProofLabel(proof: string): string {
  if (proof === 'proven' || proof === 'admin') return '已证明';
  if (proof === 'pending') return '待验证';
  return '状态未知';
}

export interface DetailInteraction {
  event_id: string;
  kind: string;
  origin: string;
  occurred_at: string | null;
}

export interface DetailMemory {
  memory_id: string;
  kind: string;
  text: string;
  visibility: string;
  created_at: string | null;
  valid_until: string | null;
}

export interface SubjectDetailItems {
  accounts: DetailAccount;
  interactions: DetailInteraction;
  memories: DetailMemory;
}

export type SubjectDetailSection = keyof SubjectDetailItems;

export interface SubjectDetailPage<T> {
  section: SubjectDetailSection;
  access: 'full' | 'public' | 'restricted' | 'purging';
  items: T[];
  next_cursor: string | null;
}
