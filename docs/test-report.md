# Test report

**Date:** 2026-10-21
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 57 of 57 checks pass across 38 scenarios (62 seconds)

## How it was tested

No real ClickUp workspace, Slack workspace or email account was used. The test script:

1. starts mock ClickUp (API v2), Slack (`chat.postMessage`), Resend (`/emails`) and Slack incoming webhook servers on the test machine,
2. imports temporary copies of the workflow and the error alert workflow, with every API host pointed at the mocks, fake keys and e2e webhook paths, and activates both,
3. sends deal, amendment and Run Now requests exactly like a CRM would, with the `X-Webhook-Secret` header,
4. waits for each n8n run to finish, then checks every request the workflow made (method, path, query, headers and body), the answer it gave and the node each run ended on,
5. switches the copy's schedule to every minute once, to test the real cron trigger and not only Run Now,
6. imports two more copies, also linked to the error alert workflow, to test a missing `WEBHOOK_SECRET` and broken settings,
7. checks that exactly the runs that should fail did fail, and that each one raised exactly one alert, matched to its run through the execution link in the alert,
8. deletes every temporary copy in a `finally` block.

### What the mocks check

The mock ClickUp keeps state like the real API: tasks and subtasks, the two custom fields, comments, `subtasks`, `include_closed`, `due_date_gt` and `due_date_lt`, ordering and paging with `last_page`. The `custom_fields` filter works the way ClickUp documents it for text fields: `==` is an exact match and `=` is a fuzzy "contains" match, so a lookup with `=` for `DEAL-100` also returns the tasks of `DEAL-1001`. Responses use the real shapes (task objects with `custom_fields`, `assignees`, `priority`, `url`; comments as `{id, hist_id, date}`; errors as `{err, ECODE}`). It rejects what the real API rejects: no `Authorization` header (`OAUTH_017`), a wrong token or a `Bearer` prefix on a personal token, a body that is not JSON, a task without a name, a due date that is not a number, an unknown parent task, an unknown custom field, assignees outside the workspace, a comment without `comment_text` or `notify_all`, and a malformed filter. It answers a 429 with `X-RateLimit-*` headers when told to, and pages at 5 tasks instead of 100 so the workflow's paging is exercised.

The mock Slack answers like the real Web API: HTTP 200 with `{"ok": false, "error": ...}` for a missing or wrong token (`not_authed`, `invalid_auth`), no channel, no text, or a refused DM (`channel_not_found`). The mock Resend checks the Bearer key and the required fields, answers 422 and 429 with Resend's error shape, and honors `Idempotency-Key` the way Resend does: the same key with the same body returns the first result without sending again; the same key with a different body is refused.

Every expected date is worked out a second time in the test script with Python's own time zone database, independently of the workflow's JavaScript.

## Found while building and testing

| Finding | What happened | Fix |
|---|---|---|
| The deal lookup read only one page | A repeated webhook for a deal with 7 tasks (2 pages in the mock) created 2 extra subtasks, because the milestones on page 2 looked missing | "ClickUp: Find Deal" now pages through every result before "Plan Changes" decides what is missing |
| n8n retries a node with all its items | In the n8n 2.19.5 execution engine, a retry runs the node again with every item, and only a failure of the first item triggers it | Every ClickUp write and every digest message goes through a loop, one request per pass. The 429 test confirms each milestone exists exactly once |
| Error workflow never ran | 7 failed runs, 0 alerts. The n8n log said "is not active and cannot be executed" | n8n 2.x needs the error workflow to be active. The setup guide says so, and the test activates its copy |

## Found in review

A second pass checked every ClickUp call against ClickUp's documentation and tightened the mock and the checks.

| Finding | What happened | Fix |
|---|---|---|
| The deal lookup used ClickUp's fuzzy match | ClickUp documents `=` on a text custom field as "contains" and `==` as exact match. The lookup used `=`, and the mock treated `=` as exact, so the test could not notice. On real ClickUp, looking up `DEAL-100` would page through every task of `DEAL-1001`, `DEAL-1002` and so on | The lookup uses `==`. The mock implements both operators, and a new scenario sends `DEAL-100` while `DEAL-1001` exists |
| Digest settings problems answered Run Now with an empty 200 | "Digest Window" threw an error before Run Now was answered, so n8n sent an empty 200, and the task runner cut the message at its ": " | Settings are checked in "Digest Window" and sent to "Respond: Digest Not Set Up" (500, every problem listed) and "Stop: Digest Not Set Up" |
| A resend with a different contract date mixed two sets of dates | The answer said to send an amendment, which cannot change the contract date, and a missing subtask would have been created from the new date | 409 and an alert, nothing written |
| An amendment could move closing before the contract date | Accepted with a 200, and the closing subtask moved to Oct 15 on a deal signed Oct 20 | "Plan Changes" refuses it with a 400 |
| Untested paths | The deal task itself failing, and a deal task with a cleared start date, had no test | Both are tested now |
| A loose error workflow check | It accepted 10 or more alerts and matched them by node name only | Exactly one alert per expected failed run, matched through the execution link, with the right Stop node and the run's own message |

The tightened test was also run once against the workflow as it was before the review: **48 of 57** checks passed. The real bugs showed up as failures: the `DEAL-100` lookup made 5 calls instead of 4 because it paged through `DEAL-1001`'s tasks, the amendment that moved closing before the contract date got a 200, the misconfigured Run Now got a 200 from "Digest Window", and the resend with a different contract date got 200 "unchanged". The other failures were the renamed nodes and reworded messages.

## Results

```
Forged request: no secret header
  PASS  refused with 401, ClickUp never called  (401, ended at Stop: Bad Secret)

Forged request: wrong secret
  PASS  refused with 401, ClickUp never called  (401, ended at Stop: Bad Secret)

Forged request: amendment
  PASS  refused with 401, ClickUp never called  (401)

Invalid deal
  PASS  refused with 400 and every problem listed, ClickUp never called  (400, 7 errors)
  PASS  closing before contract refused with 400  (["closing_date must be after contract_date"])

Invalid amendment
  PASS  a new contract date is refused with 400  (400)

New deal
  PASS  looks up the deal by its Deal ID field first, with the exact match operator  (8 ClickUp calls)
  PASS  creates the deal task: assignees, contract date as start date, closing as due date  (1428 Elm Street, Springfield, IL 62704)
  PASS  creates 6 milestone subtasks in date order with exact due dates and priorities  (Earnest money due Fri Oct 23, 2026 5:00 PM CDT, Inspection contingency ends Fri Oct 30, 2026 5:00 PM CDT, ...)
  PASS  every subtask has the parent, both assignees, a timed due date and both custom fields  (parent 86b00001)
  PASS  a deadline on a Sunday or Saturday moves to Monday and says so  (Nov 1 and Nov 7 moved)
  PASS  a deadline on a holiday moves to the next business day  (Nov 11 moved to Nov 12)
  PASS  5:00 PM local on both sides of the daylight saving change  (Oct 30 22:00 UTC, Nov 2 23:00 UTC)
  PASS  answers 200 with the task link and every deadline  (200 created)

Same deal sent twice
  PASS  nothing created the second time (the lookup reads every page first)  (2 GETs (7 tasks at 5 per page), 0 writes, 7 tasks in ClickUp)

Same deal sent again with a different closing date
  PASS  nothing changed; the answer says to send an amendment  (0 writes, 1 warning)

Same deal sent again with a different contract date
  PASS  refused with 409, nothing written, alert raised  (409, ended at Stop: Plan Rejected)

Deal ID inside another deal ID
  PASS  treated as a new deal; the lookup finds nothing from DEAL-1001  (4 ClickUp calls, 3 tasks for DEAL-100)

ClickUp refuses the deal task (500)
  PASS  retried 3 times, no subtask written, sender gets 502, alert raised  (502, 3 POSTs, ended at Stop: Deal Sync Failed)
  PASS  resending the webhook creates the deal and its subtasks once  (200 created)

Rate limited (429) on one subtask
  PASS  retried, and every milestone exists exactly once  (5 POSTs, tasks: ['closing', 'custom:seller-disclosures-due', 'deal', 'inspection'])

Cash deal
  PASS  no financing or appraisal milestone, seller agent and TC assigned  (Seller disclosures due, Inspection contingency ends, Closing)

ClickUp fails one subtask (500)
  PASS  retried 3 times, the others are created, the sender gets 502, the run raises an alert  (502, ended at Stop: Deal Sync Failed)

ClickUp is back, same webhook resent
  PASS  only the missing subtask is created  (1 POST, status repaired)

Agent not in TEAM_MEMBERS
  PASS  assigned to the transaction coordinator only, with a warning  (4 POSTs)

roll_to_business_day false
  PASS  Saturday deadlines stay on Saturday  (Oct 31 and Nov 21 are Saturdays)

Contingency after closing
  PASS  flagged in the answer  (appraisal on Nov 25, closing on Nov 21)

Amendment
  PASS  moves financing (25 days, Saturday to Monday), closing and the deal task  (2 lookup pages, then 4 writes)
  PASS  adds one comment that says exactly what moved  (comment on the deal task)
  PASS  answers 200 with the changes  (200 updated)

Same amendment sent twice
  PASS  no update and no second comment  (2 GETs, 0 writes)

Amendment adds an appraisal contingency
  PASS  creates the subtask under the existing deal and comments  (2 POSTs)

Amendment for an unknown deal
  PASS  refused with 404, nothing written, alert raised  (404, ended at Stop: Plan Rejected)

Amendment moves closing before the contract date
  PASS  refused with 400, nothing written, alert raised  (400, ended at Stop: Plan Rejected)

Deal task lost its start date
  PASS  amendment refused with 409, nothing written, alert raised  (409, ended at Stop: Plan Rejected)

ClickUp refuses the comment
  PASS  dates still answered as updated, alert carries the comment to add by hand  (ended at Stop: Deal Sync Failed)

ClickUp is down (503)
  PASS  retried 3 times, sender gets 502, nothing written, alert raised  (ended at Stop: ClickUp Failed)

Run Now with a wrong secret
  PASS  refused with 401, ClickUp never called  (401)

Digest
  PASS  asks ClickUp for open tasks and subtasks with the exact due date window, page by page  (7 open tasks in the window, pages ['0', '1'])
  PASS  reads the deal names once (paged), not once per page of milestones  (2 page(s))
  PASS  one digest per person, grouped by deal, overdue first, exactly the open milestones in the window  (Jordan Lee: 4 items, 2 overdue; Priya Shah: 2 items, 0 overdue; Casey Morgan: 6 items, 2 overdue)
  PASS  leaves out completed tasks and tasks due after the window  (checked in every digest)
  PASS  escapes deal names for Slack and for HTML email  (Loft 4 <Harbor> & Pier)
  PASS  emails go out with an Idempotency-Key, the rate limited one is retried with the same key  (4 email requests, 3 delivered)

Digest, Priya's Slack DM fails (channel_not_found)
  PASS  the others still go out, the caller gets 502, the run raises an alert  (502, ended at Stop: Digest Failed)

Digest run again
  PASS  only Priya's failed Slack DM is sent, the rest were already delivered today  (sent [('Priya Shah', 'slack')])

Digest run a third time
  PASS  nothing sent at all  (6 skipped)

Digest after an amendment
  PASS  only Jordan and Casey (on that deal) get an updated digest  (sent [('Casey Morgan', 'email'), ('Casey Morgan', 'slack'), ('Jordan Lee', 'email'), ('Jordan Lee', 'slack')])

Digest with force
  PASS  everyone gets it again, with new idempotency keys  (6 sent)

Scheduled run (real cron trigger)
  PASS  runs the same digest; only the people whose list changed get a message  (success, ended at Digest Failed?, sent 4)

ClickUp is down during the digest
  PASS  retried 3 times, caller gets 502, nothing sent, alert raised  (ended at Stop: Digest Query Failed)

WEBHOOK_SECRET not set
  PASS  deal webhook without a header is refused  (401)
  PASS  deal webhook with an empty header is refused  (401)
  PASS  Run Now with an empty header is refused  (401)

Settings missing or wrong
  PASS  deal webhook answers 500 with every setting problem, ClickUp never called  (500)
  PASS  Run Now answers 500 naming each digest setting problem, ClickUp never called  (500, ended at Stop: Digest Not Set Up)

Error workflow
  PASS  one alert per failed run, linked to that run, naming its Stop node and carrying its message  (22 failed runs, 22 expected, 22 alerts)

57/57 checks passed
```

The digest scenarios use deals dated relative to the day of the run, and the expected digests are computed from the mock's data and the window the run itself used (read from the execution), so the test passes on any day. The fixed-date deals above show up in the digest only while their dates are within the window. The n8n container's clock read Oct 20 in Chicago time during this run, which is why the digest samples below say Tuesday, Oct 20.

## What the workflow sent

Test settings: `TIMEZONE=America/Chicago`, `DEADLINE_TIME=17:00`, `HOLIDAYS=2026-11-11`, team of two agents (Jordan Lee, Priya Shah) and one coordinator (Casey Morgan). All names, addresses and IDs are made up.

**ClickUp: find deal** (`GET /api/v2/list/{list_id}/task`, then the next page until `last_page` is true)

```
subtasks=true&include_closed=true&page=0
custom_fields=[{"field_id":"5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a51","operator":"==","value":"DEAL-1001"}]
```

**ClickUp: create deal task** (`POST /api/v2/list/{list_id}/task`)

```json
{
  "name": "1428 Elm Street, Springfield, IL 62704",
  "markdown_content": "- **Deal ID:** DEAL-1001\n- **Side:** Buyer side\n- **Agent:** Jordan Lee\n- **Transaction coordinator:** Casey Morgan\n- **Contract date:** Tue Oct 20, 2026\n- **Closing:** Fri Nov 20, 2026 5:00 PM CST\n\nEach deadline is a subtask of this task. Amendments move them and leave a comment here.\n\nThe start date of this task holds the contract date that amendments count from, so please leave it as it is.",
  "assignees": [81000001, 81000009],
  "start_date": 1792515600000,
  "start_date_time": false,
  "due_date": 1795215600000,
  "due_date_time": true,
  "custom_fields": [{"id": "5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a51", "value": "DEAL-1001"}]
}
```

The start date is noon on the contract date in the office time zone, so it still reads back as the contract date even if a date-only value is shifted by a few hours.

**ClickUp: create milestone subtask** (same endpoint, with `parent`)

```json
{
  "name": "Appraisal contingency ends",
  "markdown_content": "**Deal:** DEAL-1001, 1428 Elm Street, Springfield, IL 62704\n\n**Deadline:** Mon Nov 9, 2026 5:00 PM CST\n\n**Rule:** Contract date (Tue Oct 20, 2026) + 18 days\n\nSat Nov 7, 2026 is a weekend day, so the deadline moves to the next business day.",
  "assignees": [81000001, 81000009],
  "priority": 2,
  "due_date": 1794265200000,
  "due_date_time": true,
  "custom_fields": [
    {"id": "5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a51", "value": "DEAL-1001"},
    {"id": "5f0c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a52", "value": "appraisal"}
  ],
  "parent": "86b00001"
}
```

**ClickUp: amendment** (`PUT /api/v2/task/{id}` for each moved subtask and the deal task, then `POST /api/v2/task/{deal_task_id}/comment` with `notify_all: true`)

```
Deadlines changed by amendment AMD-1.
Note from the sender: Lender asked for more time

Financing contingency ends: moved from Thu Nov 12, 2026 5:00 PM CST to Mon Nov 16, 2026 5:00 PM CST
Closing: moved from Fri Nov 20, 2026 5:00 PM CST to Wed Nov 25, 2026 5:00 PM CST
```

**Answer when one subtask could not be saved** (HTTP 502, shortened)

```json
{
  "ok": false,
  "status": "partial",
  "deal_id": "DEAL-1003",
  "task_url": "https://app.clickup.com/t/86b00012",
  "milestones": [
    {"key": "inspection", "due": "2026-11-05T17:00:00-06:00", "created": true},
    {"key": "financing", "due": "2026-11-16T17:00:00-06:00", "created": false},
    {"key": "closing", "due": "2026-12-04T17:00:00-06:00", "created": true}
  ],
  "failed": [{"key": "financing", "name": "Financing contingency ends", "error": "500 - \"{\\\"err\\\": \\\"Internal server error\\\", \\\"ECODE\\\": \\\"APP_500\\\"}\""}],
  "warnings": []
}
```

The same run ends at "Stop: Deal Sync Failed" with this message for the alert:

```
1 of 3 ClickUp changes failed for deal DEAL-1003: Financing contingency ends (500 - "{\"err\": \"Internal server error\", \"ECODE\": \"APP_500\"}"). Resend the same webhook to retry. Changes that already went through are not repeated.
```

**Answer when the deal is resent with a different contract date** (HTTP 409, the run ends at "Stop: Plan Rejected")

```json
{
  "ok": false,
  "status": "conflict",
  "deal_id": "DEAL-1001",
  "task_id": "86b00001",
  "task_url": "https://app.clickup.com/t/86b00001",
  "error": "Deal DEAL-1001 is already in ClickUp with contract date 2026-10-20, but this request says 2026-10-19. Nothing was changed. If the contract date really changed, delete the deal task and its subtasks in ClickUp and send the webhook again."
}
```

**Run Now answer when the digest settings are wrong** (HTTP 500, the run ends at "Stop: Digest Not Set Up")

```json
{
  "ok": false,
  "trigger": "run_now",
  "errors": [
    "Workflow setting: CLICKUP_LIST_ID is not set",
    "Workflow setting: DIGEST_CHANNELS can only list slack and email, not sms"
  ]
}
```

**Slack: digest DM** (`POST https://slack.com/api/chat.postMessage`, `channel` is the person's member ID, `unfurl_links` and `unfurl_media` off)

```
*Deal deadlines for Jordan Lee*, Tuesday, Oct 20, 2026
2 overdue, 2 due in the next 7 days.

*<https://app.clickup.com/t/86b0001b|Loft 4 &lt;Harbor&gt; &amp; Pier, 77 Harbor Road, Bayview, CA 94000>* (DEAL-2001)
• *OVERDUE* <https://app.clickup.com/t/86b0001c|Earnest money due>: Thu Oct 8, 5:00 PM, 12 days overdue
• *OVERDUE* <https://app.clickup.com/t/86b0001d|Inspection contingency ends>: Thu Oct 15, 5:00 PM, 5 days overdue
• <https://app.clickup.com/t/86b0001e|Closing>: Fri Oct 23, 5:00 PM, due in 3 days

*<https://app.clickup.com/t/86b00001|1428 Elm Street, Springfield, IL 62704>* (DEAL-1001)
• <https://app.clickup.com/t/86b00002|Earnest money due>: Fri Oct 23, 5:00 PM, due in 3 days
```

**Email: digest** (`POST https://api.resend.com/emails`, header `Idempotency-Key: deal-digest-2026-10-20-81000001-<content hash>`)

```json
{
  "from": "Deal Desk <deals@example.com>",
  "to": ["jordan.lee@example.com"],
  "subject": "Deal deadlines: 2 overdue, 2 due soon (Tue Oct 20)",
  "html": "<p><strong>Deal deadlines for Jordan Lee</strong>, Tuesday, Oct 20, 2026</p> ... <a href=\"https://app.clickup.com/t/86b0001b\">Loft 4 &lt;Harbor&gt; &amp; Pier, 77 Harbor Road, Bayview, CA 94000</a> ...",
  "text": "Deal deadlines for Jordan Lee, Tuesday, Oct 20, 2026\n2 overdue, 2 due in the next 7 days.\n..."
}
```

**Run Now answer when one message failed** (HTTP 502, shortened), and the next run, which sent only that message:

```json
{"ok": false, "trigger": "run_now", "date": "2026-10-20", "milestones": 6,
 "sent": [{"member": "Jordan Lee", "channel": "slack"}, {"member": "Jordan Lee", "channel": "email"}, {"member": "Priya Shah", "channel": "email"},
          {"member": "Casey Morgan", "channel": "slack"}, {"member": "Casey Morgan", "channel": "email"}],
 "failed": [{"member": "Priya Shah", "channel": "slack", "error": "Slack said channel_not_found"}]}
```

```json
{"ok": true, "trigger": "run_now", "date": "2026-10-20", "milestones": 6,
 "sent": [{"member": "Priya Shah", "channel": "slack"}],
 "skipped": [{"member": "Jordan Lee", "channel": "slack", "reason": "already sent today with the same content"}, "... 4 more"],
 "failed": []}
```
