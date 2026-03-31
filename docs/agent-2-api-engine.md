# Agent Directive: AI Orchestrator & Omnichannel Engine

**Role:** Senior AI Backend Engineer (Python/FastAPI).

**Task:** Build the core AI Engine (Pillar 1) that connects messaging platforms to our LLM and Vector DB.

**Step-by-Step Requirements:**
1.  **API Foundation:** Set up a highly secure FastAPI application. Use Pydantic for strict payload validation.
2.  **Webhooks:** Create robust, authenticated webhook endpoints for the Meta Cloud API (WhatsApp) and Telegram Bot API. Handle signature verification securely.
3.  **RAG Integration:** Construct a retrieval chain that takes incoming messages, queries the Vector DB for context, and generates an accurate response using an LLM.
4.  **Conversational Memory:** Implement user-specific memory (using Redis or SQLite/PostgreSQL) tied to their phone number/chat ID so the AI remembers previous context perfectly.
5.  **Output Formatting:** Ensure the AI can return plain text for chat apps or structured JSON if queried by our web frontend.

**Critical Constraint:** API keys must be managed strictly via `.env`. Catch all HTTP exceptions gracefully and return standardized error payloads.
