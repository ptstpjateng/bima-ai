export interface User {
  id: number;
  name: string;
  email: string;
  role: "admin" | "staff" | "msme";
  phone: string | null;
  nik: string | null;
  npwp: string | null;
  business_name: string | null;
  business_legal_type: string | null;
  business_address: string | null;
  province: string | null;
  city: string | null;
  district: string | null;
  village: string | null;
  postal_code: string | null;
  email_verified_at: string | null;
  telegram_chat_id: number | null;
  telegram_username: string | null;
}

export type PermitStatus =
  | "draft"
  | "submitted"
  | "under_review"
  | "additional_docs_required"
  | "approved"
  | "rejected"
  | "expired";

export type BusinessScale = "mikro" | "kecil" | "menengah" | "besar";

export type RiskLevel =
  | "rendah"
  | "menengah_rendah"
  | "menengah_tinggi"
  | "tinggi";

export interface PermitApplication {
  id: number;
  application_number: string;
  oss_application_number: string | null;
  nib: string | null;
  kbli_code: string;
  kbli_description: string;
  kbli_section: string | null;
  business_scale: BusinessScale;
  risk_level: RiskLevel;
  status: PermitStatus;
  business_location_address: string | null;
  business_location_province: string | null;
  business_location_city: string | null;
  annual_revenue_estimate: number | null;
  employee_count: number | null;
  documents: PermitDocument[] | null;
  requirements_checklist: RequirementItem[] | null;
  reviewer_notes: string | null;
  applicant_notes: string | null;
  submitted_at: string | null;
  reviewed_at: string | null;
  approved_at: string | null;
  rejected_at: string | null;
  permit_expiry_date: string | null;
  created_at: string;
}

export interface PermitDocument {
  name: string;
  path: string;
  type: string;
  uploaded_at?: string;
}

export interface RequirementItem {
  label: string;
  completed: boolean;
  notes?: string;
}

export type MessageType = "user_message" | "ai_response" | "system_event";

export interface AiInteraction {
  id: number;
  session_id: string;
  turn_index: number;
  channel: string;
  message_type: MessageType;
  content: string;
  intent: string | null;
  confidence_score: number | null;
  created_at: string;
}

export interface KbliOption {
  kbli_code: string;
  kbli_description: string | null;
  sector: string | null;
}

export interface ApiResponse<T> {
  success: boolean;
  message: string;
  data: T;
}

export interface AuthPayload {
  token: string;
  user: User;
}

export interface PermitApplyPayload {
  kbli_code: string;
  kbli_description: string;
  kbli_section?: string;
  business_scale: BusinessScale;
  risk_level: RiskLevel;
  business_location_address?: string;
  business_location_province?: string;
  business_location_city?: string;
  annual_revenue_estimate?: number;
  employee_count?: number;
  applicant_notes?: string;
}
