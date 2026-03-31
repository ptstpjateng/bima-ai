# Agent Directive: Core Backend & DPMPTSP Admin Dashboard

**Role:** Elite PHP/Laravel Developer (TALL Stack & Filament).

**Task:** Build the central administrative backbone for BIMA-AI. This serves as the master database and the interface for DPMPTSP officials.

**Step-by-Step Requirements:**
1.  **Project Initialization:** Scaffold a new Laravel project and install/configure Filament PHP.
2.  **Database Architecture:** Design and execute migrations for:
    *   `Users` (MSMEs with profile data).
    *   `Permit_Applications` (Tracking OSS RBA progress, status, KBLI codes).
    *   `Ai_Interactions` (Logging user chats for auditing and context).
3.  **Filament Resources:** Create clean, highly readable Filament panels for the above models. Use proper schema form validation, filters, and actions. The UI/UX must be pristine and intuitive for government staff.
4.  **API Endpoints:** Build secure, rate-limited REST API routes using Laravel Sanctum to allow the Next.js frontend and Python AI engine to read/write data safely.

**Critical Constraint:** Adhere to Laravel's best practices (Service classes, Form Requests). Implement global exception handling to ensure the API never leaks stack traces.
