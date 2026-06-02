# Curated demo packet — live content-scoring (June-4 demo)

This directory is the **provenance-staged** document set BIMA content-scores
on stage. Point `GUIDED_SUBMISSION_DEMO_PACKET` at this directory (or a copy of
it) on the demo machine:

```
GUIDED_SUBMISSION_DEMO_PACKET=/app/ai-engine/tests/fixtures/demo_packet
```

## What it is — and is not

- The **scoring is 100% real**: the bytes here are sent through
  `services/agents/suitability_judge.py` → Gemini Vision → real
  completeness / type-correctness / suitability findings.
- The **provenance is staged**: these are NOT the 1,032 real citizen files on
  Beta-SIAP. (Decisive finding: Beta's `storage/app/public` and
  `storage/app/master` hold only `.htaccess` — the uploaded PDF/image bytes
  are not on disk.) So we score a curated packet the demo "citizen" sends.

## Filename = claimed document type

Each filename's stem (underscores → spaces) is the **citizen-claimed** label
the suitability judge canonicalises and type-checks. For the demo license
"Surat Keterangan Penelitian" (Izin Penelitian, `license_id=358`) the 7
requirements are:

| Filename (suggested)             | Claimed type                       |
|----------------------------------|------------------------------------|
| `surat_permohonan_materai.pdf`   | Surat Permohonan (materai 10000)   |
| `surat_pengantar_lembaga.pdf`    | Surat Pengantar lembaga            |
| `ktp.jpg`                        | KTP                                |
| `proposal_penelitian.pdf`        | Proposal penelitian                |
| `rekomendasi_dirjen_polpum.pdf`  | Rekomendasi Dirjen Polpum          |
| `rekomendasi_dirjen_polpum_2.pdf`| Rekomendasi Dirjen Polpum (kedua)  |
| `surat_pernyataan_materai.pdf`   | Surat Pernyataan (materai 10000)   |

## Two staged scenarios

Keep two sibling directories on the demo machine and switch the env var:

- **Clean set** — all 7 present, correct types, materai visible → high score.
- **Flawed set** — one deliberately wrong-type file (e.g. an NPWP labelled
  `ktp.jpg`), one missing materai, one name mismatch → mid score with
  CRITICAL/HIGH issues. This is what makes "does-it-comply" land on stage.

## Checked-in placeholders

The two `*.pdf` files committed here are **synthetic, PII-free placeholders**
(plain text PDFs) so the loader path is exercisable in CI and the directory is
non-empty. Replace them with the real curated packet on the demo machine — do
NOT commit real or realistic PII documents to git.
