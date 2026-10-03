# Reading and project intake

Capture first, then deliberately select the initial page UUIDs for reading.
`rmk interpret UUID --page PAGE-UUID` prepares a private plan without calling a
model. Add `--execute` only within approved model-call authority. Subsequent
execution without `--page` reads new/changed pages and compares prior readings;
unchanged unselected baseline history stays unenrolled. Completed readings are
reused after interruptions. Source hashes and prompt/schema versions own identity.

Use `[broker].policy_path` pointing at Agent Broker's `policies/remarkable.yaml`.
The policy selects the central `balanced` medium brain. Broker owns current
model choices, runtime overrides and ordered Codex → Claude → governed
OpenRouter → Free fallback. Unsupported image providers fail closed. Each call
retains its real receipt, winning provider/layer, degradation and usage. A
successful provider response still must pass the app's strict reading JSON
schema before interpretation advances. Malformed readings stop visibly; they
are not fabricated or treated as accepted work.

The reading keeps full transcription private and separates a bounded extract,
uncertainty, expressed intent, reported progress, agent suggestions, changes and
dates actually written. Observation/source time never becomes writing time.

`stage-intake UUID --workspace PATH --project SLUG --page PAGE-UUID` routes the
selected batch to one existing canonical project. Omit `--page` to stage all
current interpreted pages. Monthly direction belongs in Day Planning; link
other relevant projects rather than copying the same interpretation. Existing
edited files cause a conflict. Raw transcripts, ink, renders and state stay
outside Git. Staging and source pushes do not prove shared delivery.

Run the workspace's full-candidate check and its approved publication route.
Then `confirm-intake UUID --workspace PATH --project SLUG --page PAGE-UUID
--commit FULL-SHA` verifies exact extract bytes in that canonical remote main
revision before updating delivery. Use the same page selection as staging.
Retries are idempotent; revised prompts produce a superseding reading. Git
failure leaves delivery unconfirmed. This command never approves plans, writes
Calendar/tasks, proposes memory, or sends a planning brief.

Companion Pack's `docs/remarkable-planning-input.md` owns the shared planning
input contract. Native `pulsar_context.py brief` reads bounded project intake;
hosted Pex retrieves the same published source through its existing route.
Installed profile use and natural scheduled pickup require separate proof.

## October 3 validation

The real approved Air run read four Thoughts pages using all 16 rendered image
sections: three handwritten pages and a correctly empty reading for one blank
page. All four Broker receipts succeeded through Codex at layer 0. An injected
primary failure exercised the actual Broker runner and preserved a Claude
layer-1 degraded receipt through interpretation; this is synthetic failover
proof, not a claim that the real reads needed fallback. Long-page bottom samples
were checked visually, including the incomplete `10/03` reflection.

Remarkable's full suite passed 80 tests; Career regressions passed four tests
and their subcases. Private receipts and interpretations remain under the
milestone-1 evidence directory on the Air. The scheduled oldmac service is not
activated: its installed desktop app has an empty cache and no selected
Thoughts source. Account pairing/sync and natural runtime proof remain gates.
