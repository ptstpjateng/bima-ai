# Agent Directive: OSS KBLI Dynamic Data Scraper & RAG Pipeline

**Role:** Expert Data Engineer and AI Architect (Python).

**Task:** Build a highly robust, secure, and clean Data Ingestion Pipeline for BIMA-AI. We need to dynamically scrape complex, heavily-nested business licensing data from the Indonesian OSS portal (oss.go.id).

**The Target Data Structure:**
We are targeting the KBLI details pages (e.g., KBLI 56102 -> https://oss.go.id/id/kbli/detail/eab92220-4cc3-4400-a0be-8e32da6f22a4). The script must extract:
1.  **URAIAN:** The general KBLI description.
2.  **RUANG LINGKUP (By Business Scale):** The UI has tabs for Mikro, Kecil, Menengah, and Besar. The script must click/extract data for each scale, capturing:
    * Skala
    * Luas Lahan
    * Tingkat Risiko
    * Perizinan Berusaha
    * Jangka Waktu
    * PB UMKU
3.  **Kewajiban Perizinan Berusaha:** The specific obligations.
4.  **Detailed PB UMKU:** All specific license requirements listed on the page.

**Step-by-Step Requirements:**
1.  **Environment Setup:** Generate a `requirements.txt` using `playwright` (for dynamic DOM scraping), `pydantic` (for data structuring), and `chromadb` (for vector storage).
2.  **Data Models:** Create strict Pydantic models mapping the exact target data structure above.
3.  **Dynamic Scraper Module:** Write an async Playwright script (`extractors/oss_scraper.py`) that:
    * Takes a list of KBLI codes (e.g., `["56102", "62019"]`).
    * Navigates to `https://oss.go.id/id/kbli?q={code}`.
    * Waits for the search result to render and extracts the detail page URL containing the UUID.
    * Navigates to the detail page, waits for network idle, and extracts the `URAIAN`.
    * Simulates clicks on the UI tabs (Mikro, Kecil, Menengah, Besar) to extract the nested `RUANG LINGKUP` and `Kewajiban` data.
4.  **JSON & Vector Output:** Save the highly structured Pydantic object as a local JSON file first (for debugging), then chunk it and upsert it into ChromaDB.

**Critical Code Quality:**
* **Playwright Resiliency:** Implement explicit waits (`wait_for_selector`, `wait_for_timeout`) to account for the OSS server latency.
* **Error Handling:** Use comprehensive `try/except` blocks. If KBLI "A" fails to load, log the error and gracefully continue to KBLI "B" without crashing the browser instance.
