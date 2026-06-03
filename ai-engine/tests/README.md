# ai-engine tests

Characterization + unit tests for the BIMA ai-engine. They pin down what the
code **actually does** so a refactor (or a rewrite of the WhatsApp scoring
flow) can be proven equivalent.

These tests are `unittest`-based and were originally written to run as plain
scripts (one process per file). They also run under `pytest`. Running them as
ONE aggregate (pytest, or `unittest discover`) is the stronger check because it
surfaces cross-file `sys.modules` leakage that per-file runs hide.

## What's covered (WhatsApp scoring slice)

| File | Module under test | Focus |
|---|---|---|
| `test_session_store.py` | `services/session_store.py` | flag gating, save/load/delete round-trip (fake Redis), decode-error drop, encode/decode helpers for both session dataclasses |
| `test_whatsapp_media.py` | `services/whatsapp_media.py` + `routers/aptana.py` parser | SSRF host allow-list, content-type allow-list, oversize abort, clean download, Graph-id resolve, payload parser shapes |
| `test_aptana_media_handler.py` | `routers/aptana.py::_process_inbound_media` + durable sessions + score reminder | the inbound media HANDLER end-to-end (download → attach → score → reply), the size/content-type guard replies, durable session round-trip through the **public** async API for both session types + in-memory fallback, and the REVIEW-turn score reminder via `maybe_handle` |
| `test_guided_submission_scoring.py` | `services/guided_submission.py` | attach_documents, demo-packet loader, content-score normalisation, doc-scoring transition, officer hand-off, score reminder, `handle_inbound_documents` |
| `test_officer_bridge.py` | `services/officer_bridge.py` | notify + session register, score→validation projection, officer-reply copilot bridge |
| `test_citizen_scorer.py` | `services/citizen_scorer.py` | score message rendering, readiness threshold |

`test_guided_submission.py`, `test_suitability_judge.py`,
`test_prompt_injection_defense.py` cover the rest of the flow.

## Running

No real network, Redis, Gemini, or DB are touched — heavy/uninstalled deps
(`redis`, `fastapi`, `asyncpg`, `dotenv`, and `httpx` when absent) are stubbed
in each file before the target imports.

### As scripts (no pytest needed)

```bash
cd ai-engine
python3 tests/test_aptana_media_handler.py
python3 tests/test_session_store.py
# prove no un-awaited coroutines:
python3 -W error::RuntimeWarning tests/test_aptana_media_handler.py
```

### Under pytest

`pytest` isn't in the base interpreter (PEP 668 externally-managed env). Use a
throwaway venv:

```bash
cd ai-engine
python3 -m venv .venv-test
.venv-test/bin/python -m pip install pytest pytest-asyncio httpx
.venv-test/bin/python -m pytest tests/ -q          # whole dir
.venv-test/bin/python -m pytest tests/test_aptana_media_handler.py -v
```

The `.venv-test/` dir is throwaway and NOT git-ignored by default — delete it
when done (`rm -rf .venv-test`) or add it to `.gitignore` so it isn't
committed.

#### Known-failing files (NOT this slice)

A full `pytest tests/` currently reports failures in two files that are
**unrelated** to the WhatsApp-scoring change set and fail the same way in
isolation:

* `test_siap_write.py` — collection `ModuleNotFoundError: No module named
  'dotenv'` (the file never stubs `dotenv`; install `python-dotenv` in the venv
  to collect it).
* `test_whatsapp_typing.py` — 6 failures in `AcknowledgeReceivedBranchTests`
  (tests of `acknowledge_received`, untouched by this slice).

Everything else (199 tests across the other files) passes, in any file order.

## The cross-file `sys.modules` trap (read before adding a file)

These files mutate `sys.modules` and patch already-imported names. Two patterns
matter, because getting them wrong makes a test pass alone and fail (or corrupt
a sibling) in the aggregate:

1. **Stub a dep only if it's genuinely absent — never unconditionally.**
   Use the guarded helper, NOT `if "X" not in sys.modules`:

   ```python
   def _ensure_stub(name, attrs=None):
       if name in sys.modules:
           return
       try:
           __import__(name); return        # real dep present — leave it
       except ImportError:
           pass
       mod = types.ModuleType(name); ...; sys.modules[name] = mod
   ```

   `"X" not in sys.modules` is **True at first import even when X is
   installed**, so it wrongly installs a stub that then leaks to later files
   (this exact bug made `httpx.TimeoutException` vanish for
   `test_guided_submission.py`). For `httpx`, prefer the real module and only
   patch `whatsapp_media.httpx.AsyncClient` **scoped** (`patch.object`, in a
   `with`), so no global mutation survives the test.

2. **Patch the binding the production code actually resolves.**
   `routers/aptana.py` binds `send_text` at module top
   (`from services.whatsapp_sender import send_text`), so patch
   `aptana.send_text`, not `services.whatsapp_sender.send_text`.
   `guided_submission._submit` does a lazy `from services import
   officer_bridge`; once `services.officer_bridge` is imported anywhere, that
   resolves to the **package attribute**, so a `patch.dict(sys.modules, ...)`
   alone is silently ineffective — also patch the attribute
   (`patch.object(services, "officer_bridge", stub)`).

## Adding a new characterization case

1. Pick the file for the module you're pinning (one test file per module).
2. Use literal inputs and literal expected outputs. Name the method as a spec:
   `test_oversize_download_returns_unsupported_format_reply`, not
   `test_download`.
3. The legacy/current behaviour is the oracle. If the code does something that
   looks wrong, assert what it **does today** and flag the discrepancy in the
   PR/review — don't assert the "should". (Example: the
   `_SCOREABLE_MEDIA_TYPES` import bug is pinned by
   `TestMediaHandlerImportBug`, with the intended contract documented
   separately in `TestMediaHandlerIntendedBehaviour`, guarded by injecting the
   missing symbol.)
4. Reset shared module state in `setUp` (`gs._sessions.clear()`,
   `ss._client = None`, etc.) so order can't matter.
5. Run the file alone AND `pytest tests/` before committing.
