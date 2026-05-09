# AGENTS.md

## Purpose
This repository builds design-patent image pairs by:
1. Parsing patent numbers from `.TIF` filenames.
2. Querying PatentsView for application numbers.
3. Querying USPTO cited-reference metadata.
4. Matching cited design patents to local image files.
5. Writing results to `final_pairs.json`.

Primary script: `ustpo.py`.

## Quick Start
- Install dependency: `pip install requests`
- Run: `python ustpo.py`

## Inputs and Outputs
- Input image tree root is configured in `ustpo.py` via `IMAGE_ROOT`.
- Target year folder is configured in `ustpo.py` via `TARGET_DIR`.
- Output file is `final_pairs.json` in repository root.

## Codebase Conventions
- Keep filename/patent normalization behavior stable unless explicitly requested.
- Preserve API query structure and response-field handling in:
  - `get_application_number`
  - `get_cited_prior_art`
- Keep retries/throttling behavior conservative for external APIs (`time.sleep` calls are intentional).
- Existing comments and logs are partly Japanese; preserve user-facing language style unless asked to standardize.

## Important Pitfalls
- Script currently limits indexing and processing for safety/testing:
  - Index build stops after 1000 `.TIF` files.
  - Target processing is limited to first 20 files.
- Paths are absolute and environment-specific (`/mnt/eightthdd/...`); do not generalize automatically unless asked.
- API calls require network access and may fail intermittently; avoid removing exception handling.

## Guidance for Coding Agents (Codex-focused)
- Prefer small, targeted edits in `ustpo.py`; avoid broad refactors.
- Validate behavior with a short run and confirm `final_pairs.json` shape remains unchanged.
- If changing matching logic, keep output schema keys stable:
  - `target_patent`, `target_image_path`, `application_number`, `pairs`
  - Nested pair keys: `cited_patent`, `cited_image_path`
- When introducing new dependencies, justify them and keep setup minimal.
