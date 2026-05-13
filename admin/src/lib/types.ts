/**
 * Admin-api response shapes — mirrors the contract in Sprint B.2 brief.
 * Keep in sync with admin-api OpenAPI when it lands.
 */

export type Channel = "telegram" | "whatsapp" | "web" | (string & {});
export type MessageType =
  | "user_message"
  | "ai_response"
  | "system_event"
  | (string & {});

export interface DashboardStats {
  umkm_users: number;
  whatsapp_messages_today: number;
  active_kbli_codes: number;
  pending_ingestions: number;
  chroma_chunks: number;
  last_updated: string;
}

export interface AiInteractionSummary {
  id: number | string;
  session_id: string;
  turn_index: number;
  channel: Channel;
  message_type: MessageType;
  /** Server-truncated. */
  content: string;
  intent: string | null;
  user_id: number | string | null;
  response_time_ms: number | null;
  created_at: string;
}

export interface AiInteractionTurn {
  id: number | string;
  turn_index: number;
  message_type: MessageType;
  content: string;
  intent: string | null;
  response_time_ms: number | null;
  created_at: string;
}

export interface AiInteractionSession {
  session_id: string;
  channel: Channel;
  user_id: number | string | null;
  turn_count: number;
  started_at: string;
  last_turn_at: string;
  turns: AiInteractionTurn[];
}

export interface KbliSummary {
  id: number | string;
  kode_kbli: string;
  judul_kbli: string;
  sektor: string | null;
  tingkat_risiko: string | null;
  skala_usaha: string | null;
  perizinan_berusaha: string | null;
  pb_umku_names: string[] | string | null;
}

export interface KbliDetail {
  id: number | string;
  kode_kbli: string;
  judul_kbli: string;
  ruang_lingkup: string | null;
  parameter: string | null;
  kategori: string | null;
  golongan_pokok: string | null;
  golongan: string | null;
  subgolongan: string | null;
  sektor: string | null;
  tingkat_risiko: string | null;
  skala_usaha: string | null;
  perizinan_berusaha: string | null;
  pb_umku_names: string[] | string | null;
  jangka_waktu: string | null;
  masa_berlaku: string | null;
  kewenangan: string | null;
  chunk_count: number;
}

export interface Paginated<T> {
  items: T[];
  total: number;
  page: number;
  limit: number;
}

/* ---------------------------------------------------------------------------
 * Ingestion sources (Sprint C — `/data` page).
 * Mirrors admin-api `app/schemas/ingestion.py`. Keep these literal unions in
 * sync — Pydantic enforces them server-side, but the TS literal types let us
 * narrow on the badge variant without `as` casts.
 * ------------------------------------------------------------------------ */
export type IngestionKind =
  | "pdf"
  | "excel_kbli"
  | "excel_pb_umku"
  | "url"
  | "text"
  | "docx";

export type IngestionStatus = "pending" | "processing" | "done" | "failed";

export interface IngestionSource {
  id: number;
  kind: IngestionKind;
  display_name: string;
  object_key: string | null;
  metadata_json: Record<string, unknown>;
  status: IngestionStatus;
  chunks_indexed: number;
  error: string | null;
  created_by: number | null;
  created_at: string;
  last_run_at: string | null;
}
