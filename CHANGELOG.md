# Changelog

## v1 (2026-10-21)

First build, tested end to end on n8n 2.19.5 against mock ClickUp, Slack and Resend APIs (38 scenarios, 57 checks).

### Added

- **Deal under contract webhook.** Builds one ClickUp task per deal and one subtask per milestone (inspection, appraisal and financing contingencies, closing, and custom milestones), with due dates, priorities and the agent and transaction coordinator as assignees.
- **Deadline rules.** Calendar days from the contract date, due at `DEADLINE_TIME` in `TIMEZONE`. Weekend and holiday deadlines move to the next business day (`ROLL_TO_BUSINESS_DAY`, `HOLIDAYS`, or `roll_to_business_day` per request). Daylight saving changes are handled. Milestones that end after closing are flagged.
- **ClickUp as the record.** Each task carries a Deal ID custom field and each subtask a Milestone key, and every run reads ClickUp first, using ClickUp's exact match operator. A repeated webhook creates nothing, and a run that stopped halfway is finished by sending the same webhook again. A deal resent with a different contract date is refused with a 409 instead of mixing two sets of dates.
- **Amendment webhook.** Moves the affected subtasks and the deal task, adds contingencies the deal did not have, and comments on the deal task with every date that moved. Amendments carry new totals, so a repeated delivery changes nothing. An amendment that puts closing on or before the contract date is refused with a 400.
- **Daily digest** at 7:00 plus a protected Run Now webhook. Uses ClickUp's `due_date_gt` and `due_date_lt` filters with paging, groups by person and deal, and sends a Slack direct message and an email (Resend) per person. A digest already sent today with the same content is skipped; emails carry an Idempotency-Key.
- **Safety.** A shared secret header on all three webhooks that fails closed, full request and settings validation with clear 400 and 500 answers (the digest included), retries on every API call, and a clear failure message for the error alert workflow on every failed run. Slack answers 200 even when it refuses a message (`{"ok": false}`), so its `ok` field is checked and a refused DM is retried on the next run instead of being recorded as sent.
- Sticky notes on the canvas, the error alert workflow, setup guide and an end to end mock test.

### Found while building and testing

- **The deal lookup read only the first page.** The mock pages at 5 tasks (ClickUp pages at 100), and a repeated webhook for a deal with 7 tasks created 2 extra subtasks, because the milestones on page 2 looked missing. The lookup now reads every page before deciding what is missing.
- **n8n retries a node with all of its items, and only looks at the first item to decide** (read in the n8n 2.19.5 execution engine before building). Creating several subtasks in one node would duplicate the ones that already worked when the first one hit a rate limit, and would never retry a later one. All ClickUp writes and all digest messages go through a loop, one request per pass, and the rate limit test confirms each milestone is created exactly once.
- **n8n 2.x only runs an error workflow that is active.** The first test run produced 0 of 7 alerts and the log said "is not active and cannot be executed". The setup steps now say to activate the error alert workflow, and the test does the same.

### Found in review

- **The deal lookup used ClickUp's fuzzy match.** For text custom fields, ClickUp's `=` operator means "contains" and `==` means exact match. The lookup used `=`, so looking up `DEAL-10` also returned every task of `DEAL-100` and `DEAL-1001`. The exact check in "Plan Changes" kept the data right, but the lookup paged through other deals' tasks and could hit the 50 page cap on a large list. The mock treated `=` as exact, so the test could not see it. The lookup now uses `==`, the mock implements both operators the way ClickUp documents them, and a new test sends `DEAL-100` while `DEAL-1001` exists.
- **Digest settings problems answered Run Now with an empty 200.** "Digest Window" threw an error, and n8n answers a webhook that has not been answered yet with an empty 200. The thrown messages also contained ": ", which the n8n task runner cuts. Settings are now checked in "Digest Window" (ClickUp keys, time zone, `TEAM_MEMBERS`, `DIGEST_DAYS`, `DIGEST_OVERDUE_DAYS`, and every channel in `DIGEST_CHANNELS` with its keys) and routed to "Respond: Digest Not Set Up" (500, every problem listed) and "Stop: Digest Not Set Up". A listed channel without its key used to be skipped quietly; it is now a setting problem.
- **A deal resent with a different contract date mixed two sets of dates.** The answer said "send an amendment", which cannot change the contract date, and if a subtask was missing it was created from the new date while the others kept the old one. That request now gets a 409 and an alert, and nothing is written.
- **An amendment could move closing before the contract date.** The contract date only lives in ClickUp, so "Validate Deal" could not check it. "Plan Changes" now refuses it with a 400.
- **Untested paths.** The deal task itself failing to save, and a deal task with a cleared start date (409), had no test. Both have one now.
- **The error workflow check was loose.** It accepted 10 or more alerts and matched them by node name. It now requires exactly one alert per expected failed run, matched to that run through the execution link in the alert, with the right Stop node and the run's own error message, across all three test copies.
