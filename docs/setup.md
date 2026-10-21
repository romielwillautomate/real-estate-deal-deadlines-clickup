# Setup: ClickUp, Slack, Resend and n8n

This guide connects the workflow to a real ClickUp workspace, Slack, Resend and your CRM. Plan on about 30 minutes. You can skip Slack or email if you only want one of them: set `DIGEST_CHANNELS` to `slack` or `email`. A channel that is listed but has no key stops the digest with a 500 that names the missing setting, so a forgotten key never goes unnoticed.

## 1. ClickUp

### The list

1. Pick the Space, Folder and List where your deals should live, for example a list called "Transactions". Every deal becomes one task in this list, with its milestones as subtasks.
2. In the Space settings, open **ClickApps** and check that **Multiple Assignees** and **Priority** are turned on. Without Multiple Assignees, a task can only hold one of the agent and the coordinator.
3. Open the list and copy its ID from the address bar. In `https://app.clickup.com/9011000000/v/li/901100200300` the list ID is `901100200300`. That is `CLICKUP_LIST_ID`.

### Two custom fields

The workflow finds a deal by its ID, and each milestone by its key, through two custom fields. This is what lets a repeated webhook find the existing tasks instead of creating new ones. The lookup uses ClickUp's exact match operator for text fields (`==`). ClickUp's plain `=` is a fuzzy "contains" match, which would make `DEAL-10` also find `DEAL-100`.

1. In the list, add a custom field of type **Text** named `Deal ID`.
2. Add a second **Text** field named `Milestone`.
3. Get their IDs with ClickUp's API (replace the token and list ID):

```bash
curl -s "https://api.clickup.com/api/v2/list/901100200300/field" \
  -H "Authorization: pk_YOUR_TOKEN"
```

The answer has a `fields` list. Copy the `id` of "Deal ID" into `CLICKUP_DEAL_ID_FIELD_ID` and the `id` of "Milestone" into `CLICKUP_MILESTONE_FIELD_ID`.

### The API token

1. In ClickUp, click your avatar, open **Settings**, then **Apps**, and generate an **API Token**. It starts with `pk_`. That is `CLICKUP_API_TOKEN`.
2. A personal token acts as the person who created it: tasks and comments show them as the creator. Many teams make a separate member such as "Deal Bot" for this, with access to the deals list.
3. ClickUp expects the token as the whole `Authorization` header, without `Bearer`. The workflow already sends it that way.

### User IDs for the team list

```bash
curl -s "https://api.clickup.com/api/v2/team" -H "Authorization: pk_YOUR_TOKEN"
```

Under `teams[0].members` you will find each person's `user.id` and `user.email`. Put every agent and coordinator in `TEAM_MEMBERS`:

```json
[
  {"name": "Jordan Lee", "email": "jordan.lee@example.com", "clickup_id": 81000001, "slack_id": "U0JORDAN01"},
  {"name": "Casey Morgan", "email": "casey.morgan@example.com", "clickup_id": 81000009, "slack_id": "U0CASEY009"}
]
```

The `email` is what your CRM sends as `agent_email`, so use the address the CRM knows. Set `TC_EMAIL` to the coordinator's email.

### What not to change in ClickUp

Rename tasks, change statuses, complete or reassign them as you like. The digest always reads ClickUp as it is, so a reassigned task shows up in the new person's digest. Two things should stay as the workflow set them:

- the **start date of the deal task**, which holds the contract date that amendments count from (if it is cleared, amendments are refused with a 409 until it is set back), and
- the **Deal ID and Milestone values**, which is how the workflow recognizes its own tasks.

Put notes in **comments**, not in a subtask's description: when an amendment moves a subtask, the workflow rewrites that description so the deadline rule shown there stays right. Comments are never changed.

To restart a deal from a new contract date, delete its deal task and subtasks in ClickUp and send the under contract webhook again. Resending the deal with a different contract date without deleting it is refused with a 409, so the old and new dates never get mixed.

## 2. Slack (for the digest)

1. Go to [api.slack.com/apps](https://api.slack.com/apps), choose **Create New App**, then **From scratch**, and pick your workspace.
2. Open **OAuth & Permissions** and add the bot token scope `chat:write`.
3. Click **Install to Workspace** and copy the **Bot User OAuth Token** (starts with `xoxb-`). That is `SLACK_BOT_TOKEN`.
4. For each person, open their Slack profile, click the three dots and choose **Copy member ID** (starts with `U`). Put it in their `slack_id` in `TEAM_MEMBERS`.

The digest arrives as a direct message from the app. Slack opens that conversation the first time the app writes to someone.

For failure alerts, also create an **incoming webhook** for your ops channel (in the same app under **Incoming Webhooks**, or a separate app) and put its URL in `SLACK_WEBHOOK_URL`.

## 3. Email through Resend (for the digest)

1. Create an account at [resend.com](https://resend.com).
2. Under **Domains**, add your domain and create the DNS records Resend shows you. Wait until the domain shows as verified.
3. Under **API Keys**, create a key with sending access. That is `EMAIL_API_KEY`.
4. Set `EMAIL_FROM` to a sender on the verified domain, for example `Deal Desk <deals@example.com>`.

Every digest email carries an `Idempotency-Key` header, so if n8n retries a request, Resend sends the email only once.

## 4. n8n

1. Add every variable from `.env.example` to your n8n environment (for Docker, the `environment` section or an `env_file` in your compose file) and restart n8n. Keep `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`.
2. Import `workflow/error-alert-slack.json` and **activate** it. In n8n 2.x an error workflow that is not active never runs; the n8n log then shows "is not active and cannot be executed".
3. Import `workflow/real-estate-deal-deadlines-clickup.json`.
4. Open its **Settings**: choose the error alert workflow under **Error Workflow**, and set **Timezone** to the same zone as `TIMEZONE`. The Schedule node uses the workflow time zone, so this is what makes 7:00 mean 7:00 at your office.
5. To send the digest at another time, or only on weekdays, edit the **Schedule: Daily Digest** node.
6. Activate the workflow. Your production URLs are:

| Purpose | URL |
|---|---|
| Deal under contract | `https://YOUR-N8N-DOMAIN/webhook/deal-under-contract` |
| Amendment | `https://YOUR-N8N-DOMAIN/webhook/deal-amendment` |
| Run the digest now | `https://YOUR-N8N-DOMAIN/webhook/deal-digest-run` |

Each one needs the header `X-Webhook-Secret` with the value of `WEBHOOK_SECRET`.

## 5. Connect your CRM

Anything that can send an HTTP POST with a JSON body and a custom header works: a webhook action in your CRM's automation builder, or a Zapier, Make or n8n step in between.

1. Trigger the "under contract" webhook when a deal moves to your under contract stage.
2. Map your CRM fields to the request:

| Request field | What to map | Notes |
|---|---|---|
| `deal_id` | The CRM's deal or opportunity ID | Letters, numbers, dots, dashes, underscores |
| `property_address` | The property address | Becomes the deal task name |
| `side` | Buyer side, seller side or both | `buyer`, `seller` or `dual` |
| `agent_email` | The listing or buyer's agent | Must match an email in `TEAM_MEMBERS` |
| `contract_date` | The effective date of the contract | `YYYY-MM-DD` |
| `closing_date` | The closing date in the contract | `YYYY-MM-DD` |
| `inspection_days`, `appraisal_days`, `financing_days` | The contingency periods in days | 0 or left out means no such contingency |
| `custom_milestones` | Optional extras, each with `name`, `days` and optional `priority` | For example earnest money, HOA documents |

3. Trigger the amendment webhook when an amendment is signed, with `deal_id` and only what changed: `new_closing_date`, the new total `inspection_days`, `appraisal_days` or `financing_days`, or `custom_milestones`. Add `amendment_id` and `note` so the ClickUp comment says where the change came from.
4. If your CRM can read the response, store `task_url` on the deal so agents can jump to the checklist.

## 6. Try it

Send a test deal (use your own URL and secret):

```bash
curl -s -X POST "https://YOUR-N8N-DOMAIN/webhook/deal-under-contract" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: YOUR_SECRET" \
  -d '{"deal_id":"TEST-1","property_address":"1 Test Street, Springfield, IL 62704","side":"buyer","agent_email":"jordan.lee@example.com","contract_date":"2026-10-20","closing_date":"2026-11-20","inspection_days":10,"financing_days":21}'
```

You should get `"status": "created"` with a `task_url`, and the deal appears in ClickUp with three subtasks. Send the same command again: the answer is `"status": "unchanged"` and ClickUp stays the same. Then send the digest:

```bash
curl -s -X POST "https://YOUR-N8N-DOMAIN/webhook/deal-digest-run" \
  -H "Content-Type: application/json" -H "X-Webhook-Secret: YOUR_SECRET" -d '{}'
```

Delete the test deal in ClickUp when you are done.

## Troubleshooting

Open the workflow's **Executions** list in n8n; every failed run names the node it stopped at.

| You see | Likely cause |
|---|---|
| 401 `unauthorized` | The `X-Webhook-Secret` header is missing or different, or `WEBHOOK_SECRET` is not set in n8n |
| 400 with a list of `errors` | The request itself; each problem is spelled out |
| 500 with "Workflow setting: ..." | The named variable is missing or invalid, for example a `TC_EMAIL` that is not in `TEAM_MEMBERS`, or `DIGEST_CHANNELS` lists `email` but `EMAIL_API_KEY` is not set |
| 409 `conflict` on a deal | The deal is already in ClickUp with a different contract date. See "What not to change in ClickUp" above |
| 409 `no_contract_date` on an amendment | The deal task's start date was cleared. Set it back to the contract date |
| 400 `invalid` on an amendment | The new closing date is on or before the contract date |
| 502 and "Stop: ClickUp Failed" mentioning `OAUTH_019` or `OAUTH_025` | The ClickUp token is wrong or was revoked |
| ClickUp errors mentioning `OAUTH_023`, `OAUTH_026` or `OAUTH_027` | The token's user cannot see that workspace or list, or `CLICKUP_LIST_ID` is wrong |
| ClickUp rejects the custom fields | The field IDs are wrong, or the fields were added to a different list |
| ClickUp rejects the assignees | A `clickup_id` in `TEAM_MEMBERS` is wrong |
| Only one person is assigned | The Multiple Assignees ClickApp is off for the Space |
| Deadlines are a day off | `TIMEZONE` does not match your office |
| The digest arrives at the wrong hour | The workflow's Timezone setting (step 4 of the n8n section) |
| Digest result says `Slack said channel_not_found` | The person's `slack_id` is wrong |
| Digest result says `Slack said missing_scope` or `invalid_auth` | Add `chat:write`, reinstall the app and copy the new token |
| Emails fail with `validation_error` | `EMAIL_FROM` is not on a domain verified in Resend |
| No Slack alert when a run fails | The error alert workflow is not active, or not chosen as the Error Workflow |
