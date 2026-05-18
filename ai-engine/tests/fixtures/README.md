# Validator demo fixtures

Canned `ValidateResponse` payloads for the submission completion validator
(`services/agents/validator.py`, exposed at `POST /v1/validator/submission`).

## Why these exist

Demoing the validator end-to-end on stage normally requires:

1. A real KTP/NIB/NPWP image (PII risk).
2. A live Gemini Vision call (10–30 s, costs quota, brittle on conference Wi-Fi).
3. A predictable result every time (real OCR is non-deterministic).

These fixtures let the bima-admin case page (`/admin/cases/<id>`) hit
the validator endpoint with `?demo_fixture=<scenario>` and get an
instant, deterministic response — no Gemini, no uploads, no PII.

## Scenarios

| File                 | Score | Status                       | What it shows                                                  |
|----------------------|-------|------------------------------|----------------------------------------------------------------|
| `clean.json`         | 96%   | `ready`                      | All three docs consistent — Budi Santoso, ready to submit.     |
| `name_mismatch.json` | 73%   | `needs_major_corrections`    | KTP says "BUDI SANTOSO", NIB says "BUDIE PRATAMA" → 1 HIGH.    |
| `nik_typo.json`      | 84%   | `needs_minor_corrections`    | KTP NIK is 15 digits (one missing) → 1 HIGH `nik_format`.      |

All three use the same demo persona (Budi Santoso, Semarang) so they read
as variants of the same submission rather than three unrelated people.

## Who uses them

- `bima-admin` case page → query string `?demo_fixture=clean` etc.
- `routers/validator.py` reads the query param, loads the matching JSON,
  and returns it directly — `validate_submission()` and `extract_structured()`
  are never called in this path.

## Production rollout

Demo mode is gated by `ENABLE_DEMO_FIXTURES` in `ai-engine/.env`.

- **Default = `true`** so post-deploy smoke tests and rehearsals work
  out of the box.
- **Flip to `false` before the public production cutover.** With the flag
  off, any request with `?demo_fixture=…` returns HTTP 403; the real
  Gemini Vision path is unaffected.

## Editing these

Hand-edited JSON, not generated. Schema must match `ValidateResponse` in
`routers/validator.py`. If you change the response model, re-validate each
fixture by loading it through Pydantic — there's no automated CI guard yet.
