# BIMA-AI — Hackathon Demo Script (3 minutes)

> **Demo account:** demo@bima.ai (magic link login — no password needed in demo)  
> **Pre-warm:** Send one Telegram message to @bima_ai_bot 5 minutes before demo to avoid cold start.  
> **Backup:** Use recorded video at `demo-recording.mp4` if VPS is unreachable.

---

## Setup Before Demo (5 min before)

1. Open three tabs:
   - **Tab 1:** `https://project-5z22k.vercel.app` — logged in as `demo@bima.ai`
   - **Tab 2:** Telegram — chat with `@bima_ai_bot` (pre-opened)
   - **Tab 3:** `http://116.254.113.81/admin` — Filament admin (for showing AI Interactions live)

2. Send a warm-up message on Telegram: *"halo"* — wait for response, then clear the chat.

3. Seed the demo data if not yet done:
   ```bash
   ssh bima-vps "docker exec bima-ai-backend-1 php artisan db:seed --class=DemoSeeder"
   ```

---

## Act 1 — Phase 1: Pre-License (0:00–1:00)

**Scenario:** Budi ingin membuka warung makan di Semarang. Dia tidak tahu perizinan apa yang dibutuhkan.

**[Show Telegram]**

Send on Telegram:
> *"Saya mau buka warung makan di Semarang, kira-kira perlu izin apa ya?"*

**Wait for response (~10–30s).** BIMA-AI should:
- Identify KBLI 56102 (Warung/Kedai Makan)
- Mention NIB + Sertifikat Standar (risiko Menengah Rendah)
- Include portal CTA: "Buka Portal BIMA-AI →"

**Talking points while waiting:**
> *"Di sini BIMA-AI mengenali jenis usaha Budi dan langsung mengklasifikasikan KBLI yang tepat dari 35+ kode usaha yang sudah kami indeks dari database OSS."*

---

## Act 2 — Phase 2: Execution (1:00–2:00)

**[Switch to Portal — Tab 1]**

**Scenario:** Budi mengikuti link dari BIMA-AI dan masuk ke portal.

1. Show the **Dashboard** — permit list, KBLI, status badges.
2. Click **"Ajukan Izin Baru"** → Permit Wizard opens.
3. On Step 1, type `56102` in the KBLI typeahead — show autocomplete.
4. **Skip the wizard** (demo data already seeded) — click the existing approved permit `BIMA-2026-001`.
5. Show the **Permit Detail page:**
   - Status badge: ✅ Disetujui
   - NextActionCard: "Izin Anda telah disetujui! Unduh dokumen Sertifikat Standar Anda."
   - Requirements checklist (all green)
   - Documents list

**Talking points:**
> *"Portal terintegrasi penuh dengan backend Laravel kami. Setiap permohonan divalidasi, dicatat, dan dapat dilacak real-time oleh staf DPMPTSP melalui panel admin."*

**[Briefly switch to Filament Admin — Tab 3]**

Show AI Interactions table — Budi's Telegram message logged in real-time with channel badge, intent, latency.

---

## Act 3 — Phase 3: Post-License (2:00–2:45)

**[Back to Telegram — Tab 2]**

**Scenario:** Izin sudah keluar. Budi bertanya tentang kewajiban selanjutnya.

Send on Telegram:
> *"Sertifikat Standar saya sudah keluar. Selanjutnya apa yang perlu saya lakukan?"*

**Wait for response.** BIMA-AI should mention:
- LKPM (Laporan Kegiatan Penanaman Modal) — wajib lapor tiap semester
- KUR (Kredit Usaha Rakyat) — modal kerja untuk UMKM kecil
- Kewajiban pajak (PPh final 0.5% untuk omzet <Rp 4.8M/tahun)

**Talking points:**
> *"BIMA-AI tidak berhenti di penerbitan izin. Kami mendampingi UMKM hingga fase pasca-izin — LKPM, akses modal KUR, hingga kewajiban perpajakan. Ini yang membedakan BIMA-AI dari sekadar chatbot perizinan."*

---

## Closing (2:45–3:00)

> *"Dengan BIMA-AI, kami menyederhanakan perjalanan UMKM dari nol hingga beroperasi legal — melalui Telegram, WhatsApp (segera), dan portal web. Semua terintegrasi, semua terdokumentasi, dan semua dapat diaudit oleh DPMPTSP Jawa Tengah."*

---

## Contingency Plans

| Problem | Solution |
|---|---|
| Telegram AI response >30s | Explain "model AI sedang diinisialisasi" — show the portal side while waiting |
| VPS unreachable | Play `demo-recording.mp4` for Act 1 + Act 3; portal on Vercel still works for Act 2 |
| Portal login fails | Open incognito, use magic link with `demo@bima.ai` |
| Telegram bot silent | Check `ssh bima-vps "docker logs bima-ai-ai-engine-1 --tail 20"` — likely cold start |

---

## Key Numbers to Mention

- **35+ KBLI codes** indexed from OSS database
- **3 lifecycle phases** covered (Pre-License, Execution, Post-License)  
- **2 channels** live (Telegram + Web Portal); WhatsApp in progress
- **<30s** average response time
- **100% cloud** — no GPU on-premise, runs on a single VPS
