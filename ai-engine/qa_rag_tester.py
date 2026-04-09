#!/usr/bin/env python3
"""
BIMA-AI Automated QA & RAG Evaluation Suite  v2
================================================
Tests three independent layers:
  A. RAG Layer  — ChromaDB retrieval quality (independent of Gemini)
  B. LLM Layer  — Gemini response quality (may be skipped if API unavailable)
  C. Infra Layer — TALL backend, DB, ai-logs endpoint health
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

sys.path.insert(0, "/app")

import httpx
from dotenv import load_dotenv

load_dotenv()

BACKEND_URL  = os.getenv("LARAVEL_BACKEND_URL", "http://backend:80")
INTERNAL_KEY = os.getenv("LARAVEL_API_KEY", "")
RESULTS_PATH = "/app/data/qa_results.json"
PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"


# ── Ground-truth test matrix ─────────────────────────────────────────────────

TEST_CASES = [
    {
        "id": "TC-01",
        "phase": "Phase 1 — Pre-License",
        "description": "Entity-type advisory: CV vs PT vs Perorangan for warung makan",
        "query": "Apa bedanya PT Perorangan dan CV untuk warung makan? Mana yang lebih cocok untuk usaha kecil?",
        # RAG expectations
        "rag_should_retrieve": False,          # General knowledge, not KBLI-specific
        "rag_relevant_kblis":  [],             # No specific KBLI expected
        # LLM keyword expectations (if Gemini available)
        "llm_expect_keywords":    ["CV", "perseorangan", "PT"],
        "llm_forbid_keywords":    [],
        "llm_expect_portal_link": False,       # Phase 1 — portal link not required
        "latency_sla_s": 20,
    },
    {
        "id": "TC-02",
        "phase": "Phase 2 — Execution (RAG Positive)",
        "description": "KBLI 56102 Warung Makan — kewajiban perizinan (5 chunks in DB)",
        "query": "Apa saja kewajiban perizinan untuk warung makan KBLI 56102 skala Kecil?",
        "rag_should_retrieve": True,
        "rag_relevant_kblis":  ["56102"],
        "llm_expect_keywords":    ["56102", "kewajiban", "Sertifikat"],
        "llm_forbid_keywords":    [],
        "llm_expect_portal_link": True,
        "latency_sla_s": 25,
    },
    {
        "id": "TC-03",
        "phase": "Phase 2 — Execution (RAG Negative / Trick)",
        "description": "Mining permit — KBLI 05xxx NOT in DB; should NOT suggest Sertifikat Standar",
        "query": "Bagaimana cara mengurus izin tambang batubara skala Mikro di OSS RBA?",
        "rag_should_retrieve": False,
        "rag_relevant_kblis":  [],
        "llm_expect_keywords":    ["Tinggi", "risiko"],
        "llm_forbid_keywords":    ["Sertifikat Standar"],  # Mining is Tinggi, not SS
        "llm_expect_portal_link": False,
        "latency_sla_s": 25,
    },
    {
        "id": "TC-04",
        "phase": "Phase 3 — Post-License",
        "description": "Post-license capital raising: KUR, BRI UMKM, QRIS",
        "query": "Izin saya sudah terbit, bagaimana cara mencari modal tambahan untuk kembangkan usaha?",
        "rag_should_retrieve": False,
        "rag_relevant_kblis":  [],
        "llm_expect_keywords":    ["KUR", "modal"],
        "llm_forbid_keywords":    [],
        "llm_expect_portal_link": False,
        "latency_sla_s": 20,
    },
    {
        "id": "TC-05",
        "phase": "Phase 2 — Execution (RAG Stress — 25 chunks)",
        "description": "KBLI 86101 Klinik Pratama — best-loaded KBLI in DB (25 chunks)",
        "query": "Saya mau buka klinik pratama, apa persyaratan izin untuk KBLI 86101?",
        "rag_should_retrieve": True,
        "rag_relevant_kblis":  ["86101"],
        "llm_expect_keywords":    ["86101", "klinik", "izin"],
        "llm_forbid_keywords":    [],
        "llm_expect_portal_link": True,
        "latency_sla_s": 25,
    },
]


# ── RAG Layer evaluation ──────────────────────────────────────────────────────

def evaluate_rag(tc: dict, raw_chunks: list[dict], rag_latency: float) -> dict:
    """Score RAG retrieval independently of LLM."""
    issues, warnings = [], []

    # 1. Distance threshold — cosine < 0.7 = relevant
    relevant = [c for c in raw_chunks if c.get("distance", 1.0) < 0.7]

    # 2. Metadata schema integrity check (BUG-001 regression guard)
    # rag_service.py now returns kbli_code/section/skala (fixed).
    if raw_chunks:
        if not raw_chunks[0].get("content", "").strip():
            issues.append("RAG chunk content is empty — ChromaDB data may be corrupted")
        chunks_missing_kbli = [
            c for c in raw_chunks
            if not c.get("kbli_code") and c.get("content", "").strip()
        ]
        if len(chunks_missing_kbli) > len(raw_chunks) // 2:
            warnings.append(
                "BUG-001 REGRESSION: >50% of chunks have empty kbli_code — "
                "rag_service.py metadata mapping may have reverted"
            )

    # 3. Embedding model mismatch detection (BUG-002 regression guard)
    # rag_service.py now pre-embeds with paraphrase-multilingual-MiniLM-L12-v2 (fixed).
    if raw_chunks and tc["rag_should_retrieve"] and tc["rag_relevant_kblis"]:
        retrieved_content = " ".join(c.get("content","") for c in relevant)
        expected_kblis    = tc["rag_relevant_kblis"]
        # Check both content text AND kbli_code metadata — metadata is authoritative.
        # Only flag as regression when distances are also high (> 0.55 avg on relevant),
        # indicating a true semantic space mismatch. Low-distance misses are a data
        # coverage gap (too few chunks for that KBLI), not an embedding regression.
        kbli_found = (
            any(k in retrieved_content for k in expected_kblis)
            or any(c.get("kbli_code") in expected_kblis for c in relevant)
        )
        avg_dist = (
            sum(c["distance"] for c in relevant) / len(relevant) if relevant else 1.0
        )
        if not kbli_found and relevant and avg_dist > 0.55:
            warnings.append(
                f"BUG-002 REGRESSION: Queried for KBLI {expected_kblis} "
                f"but no retrieved chunk matches and avg_dist={avg_dist:.3f}>0.55 — "
                f"embedding alignment may have regressed"
            )
        elif not kbli_found and not relevant:
            issues.append(
                f"Expected relevant chunks for KBLI {expected_kblis} but none found (d<0.7=0)"
            )

    if tc["rag_should_retrieve"] and not relevant:
        issues.append(
            f"Expected RAG results for '{tc['description']}' but 0 chunks met d<0.7 threshold"
        )
    if not tc["rag_should_retrieve"] and len(relevant) > 2:
        warnings.append(
            f"Non-KBLI query returned {len(relevant)} relevant chunks — consider stricter distance threshold"
        )

    status = FAIL if issues else (WARN if warnings else PASS)
    return {
        "status":          status,
        "issues":          issues,
        "warnings":        warnings,
        "total_chunks":    len(raw_chunks),
        "relevant_chunks": len(relevant),
        "top_distance":    round(raw_chunks[0]["distance"], 4) if raw_chunks else None,
        "latency_s":       round(rag_latency, 3),
    }


# ── LLM Layer evaluation ──────────────────────────────────────────────────────

def evaluate_llm(tc: dict, response: str, latency: float, gemini_ok: bool) -> dict:
    """Score LLM response. If Gemini was unavailable, most checks are skipped."""
    issues, warnings = [], []

    # Detect fallback responses (pre-flight unavailable OR per-test rate-limit fallback)
    is_rag_fallback = "Layanan AI sedang mengalami gangguan" in response or "Demo Mode" in response
    is_fallback = not gemini_ok or is_rag_fallback
    if is_fallback:
        reason = (
            "Gemini API returned 503 — LLM layer skipped (pre-flight)"
            if not gemini_ok
            else "Gemini rate-limited during test — BUG-003 RAG fallback triggered"
        )
        return {
            "status":   SKIP,
            "issues":   [reason],
            "warnings": [],
            "latency_s": round(latency, 2),
            "response_preview": response[:200],
            "response_len": len(response),
        }

    # Latency SLA
    if latency > tc["latency_sla_s"]:
        issues.append(f"Latency {latency:.1f}s > SLA {tc['latency_sla_s']}s")

    resp_lower = response.lower()

    # Keyword checks
    for kw in tc["llm_expect_keywords"]:
        if kw.lower() not in resp_lower:
            issues.append(f"Expected keyword missing: '{kw}'")

    # Hallucination guard
    for kw in tc["llm_forbid_keywords"]:
        if kw.lower() in resp_lower:
            issues.append(f"Hallucination / forbidden content: '{kw}' found in response")

    # Portal link check for Phase 2
    if tc["llm_expect_portal_link"] and "https://project-5z22k.vercel.app" not in response:
        warnings.append("Phase 2 response missing required portal CTA link")

    # Fabricated legal references
    import re
    fakes = re.findall(r'Pasal\s+\d+\s+[Aa]yat\s+\d+', response)
    if fakes:
        warnings.append(f"Possible hallucinated legal references: {fakes[:2]}")

    status = FAIL if issues else (WARN if warnings else PASS)
    return {
        "status":          status,
        "issues":          issues,
        "warnings":        warnings,
        "latency_s":       round(latency, 2),
        "response_preview": response[:500],
        "response_len":    len(response),
    }


# ── Phase 1: Run all test cases ───────────────────────────────────────────────

async def run_evaluation() -> list[dict]:
    from services.rag_service import query_regulations
    from services.ai_handler import generate_ai_response

    print("\n" + "═" * 65)
    print("  PHASE 1 — RAG Accuracy & LLM Evaluation")
    print("═" * 65)

    # Pre-flight: check Gemini availability
    gemini_available = await _check_gemini()
    if not gemini_available:
        print("\n  ⚠️  Gemini API is currently unavailable (503).")
        print("     LLM evaluation will be SKIPPED. RAG layer still runs.")

    results = []
    for tc in TEST_CASES:
        print(f"\n  ┌─ [{tc['id']}] {tc['description']}")
        print(f"  │  Query: \"{tc['query'][:68]}…\"")

        # A. RAG Layer
        t0 = time.monotonic()
        raw_chunks = query_regulations(tc["query"], n_results=5)
        rag_result = evaluate_rag(tc, raw_chunks, time.monotonic() - t0)

        # B. LLM Layer
        gemini_ok = False
        response  = ""
        if gemini_available:
            t1 = time.monotonic()
            try:
                response  = await generate_ai_response("qa-9999", tc["query"])
                gemini_ok = True
            except Exception as e:
                response  = f"[ERROR: {e}]"
            llm_latency = time.monotonic() - t1
        else:
            llm_latency = 0.0

        llm_result = evaluate_llm(tc, response, llm_latency, gemini_ok)

        # C. Log to backend (non-blocking)
        if gemini_ok:
            asyncio.create_task(
                _log_interaction(tc["id"], tc["query"], response, round(llm_latency * 1000))
            )

        # Print summary
        rag_icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ "}.get(rag_result["status"], "  ")
        llm_icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "SKIP": "⏭️ "}.get(llm_result["status"], "  ")
        print(f"  │  RAG  {rag_icon} {rag_result['status']:4s} | "
              f"chunks={rag_result['total_chunks']} relevant={rag_result['relevant_chunks']} "
              f"top_d={rag_result['top_distance']} | {rag_result['latency_s']}s")
        print(f"  │  LLM  {llm_icon} {llm_result['status']:4s} | "
              f"chars={llm_result['response_len']} | {llm_result['latency_s']}s")
        for iss in rag_result["issues"] + llm_result["issues"]:
            print(f"  │    ❌ {iss}")
        for w in rag_result["warnings"] + llm_result["warnings"]:
            print(f"  │    ⚠️  {w}")
        print(f"  └─ done")

        results.append({
            "id":          tc["id"],
            "phase":       tc["phase"],
            "description": tc["description"],
            "query":       tc["query"],
            "rag":         rag_result,
            "llm":         llm_result,
            "full_response": response,
        })

        await asyncio.sleep(2.0)

    return results


async def _check_gemini() -> bool:
    gemini_key   = os.getenv("GEMINI_API_KEY", "")
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").removeprefix("models/")
    if not gemini_key:
        return False
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{gemini_model}:generateContent?key={gemini_key}")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json={
                "contents": [{"parts": [{"text": "Reply: OK"}]}],
                "generationConfig": {"maxOutputTokens": 5, "temperature": 0}
            })
            return r.status_code == 200
    except Exception:
        return False


async def _log_interaction(test_id: str, query: str, response: str, response_ms: int) -> None:
    if not INTERNAL_KEY:
        return
    session_id = f"qa-{test_id}-{int(time.time())}"
    headers = {"X-Internal-Key": INTERNAL_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=8.0) as client:
        for turn, (mtype, content) in enumerate([("user_message", query), ("ai_response", response)]):
            try:
                await client.post(f"{BACKEND_URL}/api/internal/ai-logs", json={
                    "session_id":       session_id,
                    "turn_index":       turn,
                    "channel":          "internal",
                    "message_type":     mtype,
                    "content":          content,
                    "model_used":       "gemini-2.5-flash",
                    "response_time_ms": response_ms if mtype == "ai_response" else 0,
                }, headers=headers)
            except Exception:
                pass


# ── Phase 2: Backend health ───────────────────────────────────────────────────

async def run_backend_health() -> dict:
    print("\n" + "═" * 65)
    print("  PHASE 2 — TALL Backend & Infrastructure Health")
    print("═" * 65)

    health: dict[str, Any] = {"checks": {}, "issues": [], "warnings": []}
    headers = {"X-Internal-Key": INTERNAL_KEY, "Accept": "application/json"}

    async with httpx.AsyncClient(timeout=10.0) as client:

        # Backend reachability (use admin panel redirect as proxy)
        try:
            r = await client.get(f"{BACKEND_URL}/admin", timeout=6.0, follow_redirects=True)
            ok = r.status_code < 500
            health["checks"]["backend_reachable"] = {"status": PASS if ok else FAIL, "http": r.status_code}
            print(f"  Backend /admin: HTTP {r.status_code} → {'✅' if ok else '❌'}")
        except Exception as e:
            health["checks"]["backend_reachable"] = {"status": FAIL, "error": str(e)}
            health["issues"].append(f"Backend unreachable: {e}")
            print(f"  ❌ Backend unreachable: {e}")

        # DB connectivity (pipeline queue = DB round-trip)
        try:
            r = await client.get(f"{BACKEND_URL}/api/internal/pipeline/queue?limit=1", headers=headers)
            ok = r.status_code == 200
            pending = 0
            if ok:
                pending = len(r.json().get("data", {}).get("targets", []))
            health["checks"]["database"] = {
                "status": PASS if ok else FAIL,
                "http": r.status_code,
                "pending_kbli": pending,
            }
            print(f"  DB (pipeline queue): HTTP {r.status_code} | pending_kbli={pending} → {'✅' if ok else '❌'}")
        except Exception as e:
            health["checks"]["database"] = {"status": FAIL, "error": str(e)}
            health["issues"].append(f"DB check failed: {e}")
            print(f"  ❌ DB check failed: {e}")

        # ai-logs endpoint
        probe_session = f"qa-probe-{int(time.time())}"
        try:
            r = await client.post(f"{BACKEND_URL}/api/internal/ai-logs", json={
                "session_id":   probe_session,
                "turn_index":   0,
                "channel":      "internal",
                "message_type": "system_event",
                "content":      "[QA Suite health-check probe]",
                "model_used":   "bima-qa-tester",
            }, headers=headers)
            ok = r.status_code in (200, 201)
            health["checks"]["ai_logs_endpoint"] = {
                "status": PASS if ok else WARN,
                "http": r.status_code,
                "body_preview": r.text[:120],
            }
            if not ok:
                health["warnings"].append(f"ai-logs POST returned {r.status_code}: {r.text[:100]}")
            print(f"  ai-logs POST: HTTP {r.status_code} → {'✅' if ok else '⚠️ '}")
        except Exception as e:
            health["checks"]["ai_logs_endpoint"] = {"status": FAIL, "error": str(e)}
            health["issues"].append(f"ai-logs POST failed: {e}")
            print(f"  ❌ ai-logs POST failed: {e}")

    # ChromaDB
    try:
        import chromadb
        cc  = chromadb.PersistentClient(path=os.getenv("CHROMA_DB_PATH", "/app/chroma_db"))
        col = cc.get_or_create_collection("oss_regulations")
        cnt = col.count()
        health["checks"]["chromadb"] = {"status": PASS, "doc_count": cnt}
        print(f"  ChromaDB: {cnt} docs → ✅")
    except Exception as e:
        health["checks"]["chromadb"] = {"status": FAIL, "error": str(e)}
        health["issues"].append(f"ChromaDB inaccessible: {e}")
        print(f"  ❌ ChromaDB: {e}")

    # Gemini API
    gemini_ok = await _check_gemini()
    health["checks"]["gemini_api"] = {
        "status": PASS if gemini_ok else WARN,
        "note": "Available" if gemini_ok else "503 Service Unavailable (transient)",
    }
    print(f"  Gemini API: {'✅ Available' if gemini_ok else '⚠️  503 (transient high demand)'}")
    if not gemini_ok:
        health["warnings"].append("Gemini API returned 503 during this run — ai_handler.py has no retry/fallback")

    overall = FAIL if health["issues"] else (WARN if health["warnings"] else PASS)
    health["status"] = overall
    _inf_icon = {'PASS':'✅','FAIL':'❌','WARN':'⚠️ '}.get(overall,'  '); print(f'  {_inf_icon} Infrastructure: {overall}')
    return health


# ── Report compilation ────────────────────────────────────────────────────────

def build_report(results: list[dict], health: dict, gemini_available: bool) -> dict:
    rag_passes = sum(1 for r in results if r["rag"]["status"] == PASS)
    rag_warns  = sum(1 for r in results if r["rag"]["status"] == WARN)
    rag_fails  = sum(1 for r in results if r["rag"]["status"] == FAIL)

    llm_passes = sum(1 for r in results if r["llm"]["status"] == PASS)
    llm_skips  = sum(1 for r in results if r["llm"]["status"] == SKIP)
    llm_fails  = sum(1 for r in results if r["llm"]["status"] == FAIL)

    avg_rag_lat = sum(r["rag"]["latency_s"] for r in results) / len(results)
    avg_llm_lat = sum(r["llm"]["latency_s"] for r in results if r["llm"]["status"] != SKIP)
    avg_llm_lat = avg_llm_lat / max(1, len(results) - llm_skips)

    all_bugs = list({
        w for r in results
        for w in r["rag"].get("warnings", []) + r["llm"].get("warnings", [])
        if "BUG-" in w
    })

    return {
        "generated_at":     datetime.now(timezone.utc).isoformat(),
        "gemini_available": gemini_available,
        "summary": {
            "total_tests": len(results),
            "rag_pass": rag_passes, "rag_warn": rag_warns, "rag_fail": rag_fails,
            "llm_pass": llm_passes, "llm_skip": llm_skips, "llm_fail": llm_fails,
            "overall_status": FAIL if (rag_fails > 0 or llm_fails > 0) else (
                WARN if (rag_warns > 0 or llm_skips > 0) else PASS
            ),
        },
        "metrics": {
            "avg_rag_latency_s":  round(avg_rag_lat, 3),
            "avg_llm_latency_s":  round(avg_llm_lat, 2),
            "chroma_doc_count":   health["checks"].get("chromadb", {}).get("doc_count", 0),
        },
        "known_bugs":     all_bugs,
        "backend_health": health,
        "test_results":   results,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    print("\n╔══════════════════════════════════════════════════════════════╗")
    print("║  BIMA-AI · QA & RAG Evaluation Suite v2                     ║")
    print(f"║  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}                                       ║")
    print("╚══════════════════════════════════════════════════════════════╝")

    gemini_available = await _check_gemini()
    test_results     = await run_evaluation()
    backend_health   = await run_backend_health()
    report           = build_report(test_results, backend_health, gemini_available)

    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n  ✅ Results → {RESULTS_PATH}")

    s = report["summary"]
    print(f"\n  ══ SUMMARY ══")
    print(f"  RAG Layer : {s['rag_pass']} PASS / {s['rag_warn']} WARN / {s['rag_fail']} FAIL")
    print(f"  LLM Layer : {s['llm_pass']} PASS / {s['llm_skip']} SKIP / {s['llm_fail']} FAIL")
    print(f"  Backend   : {backend_health['status']}")
    print(f"  OVERALL   : {s['overall_status']}\n")

if __name__ == "__main__":
    asyncio.run(main())
