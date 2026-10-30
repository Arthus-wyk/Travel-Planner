# Guided Travel Planning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Deliver the approved form-led travel workflow with simulated candidates, guide OCR, LangGraph main/subagent coordination, audit, itinerary sheet and purchase records.

**Architecture:** Store wizard state in a JSON field on existing travel plans. A compiled LangGraph coordinator invokes scoped specialist functions and returns structured results; the API handles persistence and traveler decisions. The existing chat graph remains available for edits.

**Tech Stack:** Python 3.10, LangGraph, MySQL, React/Vite, Google Maps when configured, local OCR runtime.

**Spec:** `docs/superpowers/specs/2026-10-28-guided-travel-planning-design.md`

## Global Constraints

- Simulated flight, train and hotel prices must always be labeled and reproducible.
- No booking or payment side effects.
- Preserve existing plans, purchases and LangGraph chat behavior.
- Confirmation is blocked by high-severity audit findings.

## Review Focus

- Old plans without planning JSON still load and render.
- Invalid candidate IDs cannot be selected or marked purchased.
- OCR failure never produces invented guide text.
- Same simulated query gives the same prices.
- Replanning cannot silently delete a purchased item.

---

### Task 1: Durable planning state and candidate pricing

**Files:** `service/storage/travel_db.py`, `service/travel/planning.py`, `tests/test_guided_planning.py`

**Interfaces:** `create_draft(form)`, `build_candidates(plan)`, `select_candidates(plan, selection)`, persisted `plan["planning"]`.

- [x] Write tests for form validation, legacy plan loading, stable simulated prices and candidate selection.
- [x] Run the tests and confirm expected failures.
- [x] Add the planning JSON column, services and deterministic candidate generation.
- [x] Run focused tests and the existing Python suite.

### Task 2: Guide text and activities

**Files:** `service/travel/guide.py`, `service/travel/planning.py`, `tests/test_guided_planning.py`

**Interfaces:** `recognize_guide(image_bytes)`, `analyze_guide(text)`, `build_activities(plan, guide_text)`, `select_activities(plan, choices)`.

- [x] Write failing tests for OCR failure, extracted place review and activity choices.
- [x] Implement OCR and activity candidate services without inventing recognized text.
- [x] Run focused tests and the existing Python suite.

### Task 3: Route draft, audit and purchase accounting

**Files:** `service/travel/planning.py`, `service/travel/itinerary_audit.py`, `tests/test_guided_planning.py`

**Interfaces:** `draft_itinerary(plan)`, `audit_plan(plan)`, `record_purchase(plan, purchase)`, `itinerary_sheet(plan)`.

- [x] Write failing tests for day anchors, budget overrun, bought-item preservation and paid-price accounting.
- [x] Implement the route, audit and purchase services.
- [x] Run focused tests and the existing Python suite.

### Task 4: LangGraph coordinator and HTTP endpoints

**Files:** `agent/planning_graph.py`, `service/api/server.py`, `tests/test_guided_api.py`

**Interfaces:** `run_planning_action(plan, action, payload)`; `/api/plans/drafts` and `/api/plans/{id}/planning/*`.

- [x] Write failing endpoint and graph routing tests.
- [x] Add coordinator/specialist nodes and API persistence/error handling.
- [x] Run focused tests and the existing Python suite.

### Task 5: Guided React screens and printable itinerary

**Files:** `frontend/src/App.jsx`, `frontend/src/styles.css`, `README.md`.

**Interfaces:** Form, candidate/guide/activity selectors, audit view, purchase fields and printable itinerary sheet using the new API.

- [x] Implement the UI using native forms and current API helper.
- [x] Run `npm run build` and inspect the rendered flow when a browser is available.
- [x] Run the full Python suite and build again, then audit every spec requirement.

