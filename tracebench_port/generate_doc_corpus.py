#!/usr/bin/env python3
"""
generate_doc_corpus.py

Synthesise a documentation corpus for the PaaS diagnostic benchmark.

Produces three document types per incident:
  - runbook_{inc_id}.md       : step-by-step investigation procedure
  - error_ref_{inc_id}.md     : key log message patterns + meanings
  - config_{inc_id}.md        : configuration parameters at root of incident

Plus 5 cross-cutting architecture reference docs derived from platform doc:
  - arch_control_data_plane.md
  - arch_failure_propagation.md
  - arch_component_dependencies.md
  - arch_operating_baselines.md
  - arch_observability_pipeline.md

Each document is written as a JSONL record:
  {
    "doc_id":          "runbook_INC-008",
    "doc_type":        "runbook",          # runbook | error_ref | config | architecture
    "incident_ids":    ["INC-008"],        # ground-truth relevance — used for eval
    "components":      ["APP", "METRICS", "ROUTER"],
    "failure_pattern": "connection_pool_exhaustion",
    "tier":            2,
    "title":           "...",
    "content":         "full markdown text of the document"
  }

Output: ./data/doc_corpus.jsonl
"""

import json
import os

os.makedirs("./data", exist_ok=True)

docs = []

def doc(doc_id, doc_type, incident_ids, components, failure_pattern, tier, title, content):
    docs.append({
        "doc_id":          doc_id,
        "doc_type":        doc_type,
        "incident_ids":    incident_ids,
        "components":      components,
        "failure_pattern": failure_pattern,
        "tier":            tier,
        "title":           title,
        "content":         content.strip(),
    })


# ============================================================================
# CROSS-CUTTING ARCHITECTURE DOCS  (relevant to multiple incidents)
# ============================================================================

doc(
    doc_id="arch_control_data_plane",
    doc_type="architecture",
    incident_ids=["INC-007", "INC-009", "INC-013", "INC-016", "INC-018", "INC-021"],
    components=["CONTROLLER", "SCHEDULER", "ROUTER", "CELL", "MESSAGE_BUS"],
    failure_pattern="architectural_reference",
    tier=1,
    title="Platform Architecture: Control Plane vs Data Plane",
    content="""
# Platform Architecture: Control Plane vs Data Plane

## Overview

The platform is divided into two largely independent planes. This split is the
single most important concept for correct log diagnosis.

## Control Plane

**Components:** CONTROLLER, SCHEDULER (BBS/Locket), MESSAGE_BUS (NATS), AUTOSCALER

**Purpose:** Manages desired state — scheduling, routing updates, scaling decisions,
deployments.

**Failure effect:** New deployments fail; running apps are unaffected.

**Log indicators:** Errors in SCHEDULER, CONTROLLER; 'cf push rejected'.

**Key examples:**
- INC-021: BBS quorum loss stops all cf push operations — running routes stay healthy.
- INC-013: SERVICE_BROKER timeout blocks deploy pipeline — running apps unaffected.

## Data Plane

**Components:** CELL, ROUTER, NETWORK, APP, HEALTH

**Purpose:** Serves live traffic — HTTP routing to app instances, container execution.

**Failure effect:** Live traffic is disrupted; requests fail or are mis-routed.

**Log indicators:** Errors in ROUTER (502/503), CELL (container killed), APP.

**Key examples:**
- INC-009: Cell OOM kills all co-located instances immediately.
- INC-018: Stale route cache causes 502s after blue-green swap.

## Diagnostic Rule

When running applications serve traffic normally but new deployments fail:
→ Suspect the **control plane** (SCHEDULER, CONTROLLER, MESSAGE_BUS).

When live requests are failing:
→ Suspect the **data plane** (CELL, ROUTER, NETWORK, APP).

This distinction prevents a common mistake: seeing 503s from ROUTER and
assuming the problem is in ROUTER, when the root cause is a SCHEDULER quorum
loss (INC-021) or NATS saturation (INC-016) that happened earlier.
""",
)

doc(
    doc_id="arch_failure_propagation",
    doc_type="architecture",
    incident_ids=["INC-008", "INC-009", "INC-016", "INC-022", "INC-025"],
    components=["CELL", "ROUTER", "MESSAGE_BUS", "APP", "METRICS", "SCHEDULER"],
    failure_pattern="architectural_reference",
    tier=2,
    title="Platform Architecture: Failure Propagation Patterns",
    content="""
# Platform Architecture: Failure Propagation Patterns

Platform failures follow predictable propagation paths. Recognising the pattern
from the first few log entries allows prediction of downstream symptoms and —
critically — identification of which symptoms are consequences rather than causes.

## Cell Resource Exhaustion

**Trigger:** Cell memory or CPU reaches critical threshold.

**Propagation:** Cell metric warning → Cell OOM/CPU critical → ALL co-located
instances killed simultaneously → SCHEDULER reschedules → Secondary cell pressure
→ Health check timeouts → Partial or full app unavailability.

**Key characteristic:** All instances on the affected cell crash within milliseconds
of each other. Simultaneous crashes distinguish cell failure from an app bug
(which would crash instances one at a time, randomly).

**Red herring:** HEALTH timeouts and ROUTER errors appear last and look like the
problem. The actual cause is the METRICS cell OOM event that occurred earlier.

**Relevant incidents:** INC-009, INC-017.

## Connection Pool Exhaustion

**Trigger:** Slow queries hold open database connections until pool is full.

**Propagation:** Slow queries → Pool utilisation climbs → Pool reaches max →
New requests immediately time out → 503s → Autoscaler scale-out → New instances
hit same exhausted pool → Scaling provides zero relief.

**Key characteristic:** Scaling the app does not help. 503s that persist after
a scale-out event indicate a shared resource bottleneck (DB pool, external API).

**Red herring:** Autoscaler scale-out looks like a remediation action. It is a
distractor — new instances share the same exhausted pool.

**Relevant incidents:** INC-008.

## NATS Message Bus Saturation

**Trigger:** Runaway publisher floods NATS above subscriber capacity.

**Propagation:** Runaway publisher → NATS drops slow consumers (gorouter, Diego)
→ Routing table stops updating → Cell heartbeats stop arriving → Scheduler marks
healthy cells as suspect → Diego evacuates LRPs from healthy cells.

**Key characteristic:** Multiple downstream components appear to fail simultaneously
(stale routing table, missing cell heartbeats, cell evacuation) but none have
errors in their own internal logs. The common upstream cause is MESSAGE_BUS
saturation.

**Red herring:** Cell evacuation looks like a cell failure. The cell is entirely
healthy — it is being evacuated because the scheduler lost its heartbeat due to
NATS saturation, not because the cell had a problem.

**Relevant incidents:** INC-016.

## External Dependency Cascade

**Trigger:** External service becomes slow, holding app threads.

**Propagation:** Slow external call → App holds threads → Thread pool fills →
New requests rejected immediately → App appears completely unresponsive →
Health check times out (uses same thread pool) → App marked unhealthy → 503s.

**Key characteristic:** The app appears completely dead, but some requests still
succeed (those that bypass the slow external call — e.g. cached responses).
Partial success is the signature of thread pool exhaustion rather than a process
crash.

**Red herring:** Cached requests return 200 OK during the outage. Concluding the
app is 'partially healthy' misses the thread exhaustion mechanism entirely.

**Relevant incidents:** INC-022.

## Certificate Expiry

**Trigger:** Certificate expires after a warning period.

**Propagation:** Warning logged hours before expiry → Gap of routine operation
→ Certificate expires → ALL connections using that certificate fail simultaneously.

**Key characteristic:** Unlike failures that escalate gradually, certificate expiry
causes an abrupt cliff: 100% success rate until the expiry moment, then 100%
failure rate immediately. Simultaneity of failures across all services using the
cert distinguishes this from a deployment error.

**Preceding signal:** CERT WARNING log entries appear before the outage. In INC-025,
these appear 6h12m before the failure. The warning and failure are causally
connected despite the time gap.

**Relevant incidents:** INC-005, INC-025.

## Autoscaler Oscillation

**Trigger:** Cooldown period shorter than instance startup time.

**Propagation:** CPU exceeds threshold → Scale-up → New instance starts (55-65s)
→ Instance absorbs load, CPU drops → Cooldown (30s) expires before instance
proven → Scale-down → CPU spikes → Cycle repeats.

**Root cause:** cooldown_period < instance_startup_time.

**Relevant incidents:** INC-020.
""",
)

doc(
    doc_id="arch_component_dependencies",
    doc_type="architecture",
    incident_ids=["INC-001", "INC-003", "INC-007", "INC-010", "INC-013", "INC-016", "INC-021", "INC-023"],
    components=["CONTROLLER", "SCHEDULER", "ROUTER", "CELL", "MESSAGE_BUS", "BLOB_STORE",
                "SERVICE_BROKER", "NETWORK", "BUILD_SERVICE", "HEALTH", "METRICS", "AUTOSCALER"],
    failure_pattern="architectural_reference",
    tier=1,
    title="Platform Architecture: Component Dependencies",
    content="""
# Platform Architecture: Component Dependencies

Use this table when a component shows an error and you need to know which
upstream component to investigate as the potential cause, or which downstream
components might be secondarily affected.

## CONTROLLER (Cloud Controller API)
- **Receives from:** cf CLI (operator)
- **Calls / affects:** SERVICE_BROKER, SCHEDULER, AUTOSCALER
- **Failure impact:** If SCHEDULER unavailable, cf push fails at placement step.
  If SERVICE_BROKER times out, deploy blocked at credential bind step.

## SCHEDULER (Diego BBS + Auctioneer)
- **Receives from:** CONTROLLER (deploy requests), MESSAGE_BUS (heartbeats)
- **Calls / affects:** CELL (placement)
- **Failure impact:** Requires Locket quorum (2/3 nodes). Loss of quorum stops all
  placements. Does NOT affect running instances.

## MESSAGE_BUS (NATS)
- **Receives from:** All platform components (publish)
- **Calls / affects:** ROUTER (route table), SCHEDULER (heartbeats), CELL (registration)
- **Failure impact:** Saturation causes all subscribers to lag. NATS does not crash —
  it drops slow consumers. Routing table becomes stale, cell heartbeats go missing.

## CELL (Diego Rep)
- **Receives from:** SCHEDULER (placement), BLOB_STORE (droplet download)
- **Calls / affects:** APP (runs inside container), HEALTH (probed by)
- **Failure impact:** OOM on a cell kills ALL co-located containers immediately.
  CPU exhaustion throttles all containers proportionally.

## ROUTER (Gorouter)
- **Receives from:** MESSAGE_BUS (route registrations via NATS)
- **Calls / affects:** APP (receives forwarded requests)
- **Failure impact:** Route table has ~30s TTL per cache entry. Stale entries cause
  502s after route changes. No healthy backends = 503.

## HEALTH (Diego Health Checker)
- **Receives from:** (internal, polls app instances)
- **Calls / affects:** SCHEDULER (health status), ROUTER (gates traffic)
- **Failure impact:** Liveness failure → CELL restarts container. Readiness failure →
  ROUTER withholds traffic. Both can block rolling deploys.

## NETWORK (Silk CNI)
- **Receives from:** (kernel-level enforcement)
- **Calls / affects:** APP (enforces on outbound calls)
- **Failure impact:** Policies are DENY-by-default between apps. Missing
  'cf add-network-policy' produces ECONNREFUSED at the caller APP, logged as
  DENY by NETWORK on the receiving cell.

## BUILD_SERVICE (Buildpack Staging)
- **Receives from:** CONTROLLER (triggered by push)
- **Calls / affects:** BLOB_STORE (uploads droplet)
- **Failure impact:** Build success does not guarantee runtime success. Version
  downgrades by buildpack produce startup crashes, not build failures.

## BLOB_STORE
- **Receives from:** BUILD_SERVICE (upload), CELL (download)
- **Calls / affects:** (none)
- **Failure impact:** Partial uploads from interrupted connections are not cleaned up
  automatically. They block future deploys with checksum errors.

## SERVICE_BROKER (Open Service Broker API)
- **Receives from:** CONTROLLER (bind/unbind requests)
- **Calls / affects:** APP (credentials via VCAP_SERVICES)
- **Failure impact:** Broker timeout blocks the deploy pipeline. App never gets
  credentials; staging cannot proceed.

## METRICS (Loggregator Pipeline)
- **Receives from:** All components (loggregator agents collect)
- **Calls / affects:** AUTOSCALER (consumes metrics), Log drains
- **Failure impact:** Serial pipeline: agents → doppler → RLP → consumers.
  Doppler buffer saturation causes silent drops; consumers see metric gaps.

## AUTOSCALER
- **Receives from:** METRICS (reads CPU/memory via RLP)
- **Calls / affects:** CONTROLLER (issues scale commands)
- **Failure impact:** Scaling is not a fix for bottlenecks in shared resources
  (DB pools, NATS). Scale commands go through CONTROLLER — if CONTROLLER is
  degraded, scale commands may be delayed.
""",
)

doc(
    doc_id="arch_operating_baselines",
    doc_type="architecture",
    incident_ids=["INC-008", "INC-009", "INC-015", "INC-016", "INC-017", "INC-018",
                  "INC-020", "INC-022"],
    components=["METRICS", "ROUTER", "CELL", "APP", "MESSAGE_BUS", "AUTOSCALER"],
    failure_pattern="architectural_reference",
    tier=1,
    title="Platform Architecture: Normal Operating Baselines",
    content="""
# Platform Architecture: Normal Operating Baselines

Deviations from these values are diagnostic signals. Several incidents log the
normal value explicitly in the message (e.g. 'normal: 2000/s') — use those
annotations as ground truth for that incident's baseline context.

| Metric                        | Normal          | Alert Threshold       | Benchmark Example |
|-------------------------------|-----------------|-----------------------|-------------------|
| NATS message rate             | ~2,000 msg/s    | >10,000 msg/s         | INC-016: 48,000 msg/s from runaway metrics-agent |
| OAuth validation latency      | ~80ms           | >500ms                | INC-022: escalated to 3,800ms before thread exhaustion |
| DB connection pool usage      | <70% of max     | >85% sustained        | INC-008: pool hit 20/20 (100%) causing TimeoutErrors |
| Health check response time    | <1,000ms        | >3,000ms              | INC-017: 4,100ms on CPU-starved cell, timed out at 5,000ms |
| Doppler buffer utilisation    | <30%            | >70%                  | INC-015: reached 98%, triggering envelope drops |
| Cell memory available         | >20% of capacity| <10% of capacity      | INC-009: cell-009 dropped to 512MB of 64GB before OOM |
| Cell CPU utilisation          | <70%            | >85% sustained        | INC-017: cell-012 at 94%, throttling co-located apps |
| Container disk usage          | <70% of quota   | >80% of quota         | INC-002: climbed to 99.9% before disk_quota_exceeded kill |
| App memory (% of quota)       | <75%            | >85%                  | INC-004: 85% with GC warnings, OOM killed at 99% |
| Autoscaler cooldown period    | Must exceed startup time | <instance startup | INC-020: 30s cooldown vs 55-65s startup = oscillation |
| Gorouter cache TTL            | ~30 seconds     | N/A (fixed value)     | INC-018: stale cache caused 502s for 45s after route swap |
| Instance startup time         | 15-60s typical  | N/A                   | INC-020: new instances took 55-65s to absorb load |

## How to Use These Baselines

When a log message contains a metric value, compare it to the normal range above.
For example:
- `db_pool_active_connections=18/20` → 90% utilisation → above the 85% alert threshold → investigate for INC-008 pattern.
- `NATS: message rate=48000/s` → 24x normal → investigate for INC-016 pattern.
- `Doppler buffer: 98% full` → above 70% threshold → investigate for INC-015 pattern.

The baselines table is also useful for identifying false positives: a metric at
72% of threshold is not yet an alert and should not be cited as a root cause.
""",
)

doc(
    doc_id="arch_observability_pipeline",
    doc_type="architecture",
    incident_ids=["INC-015", "INC-016", "INC-020"],
    components=["METRICS", "AUTOSCALER", "ROUTER"],
    failure_pattern="architectural_reference",
    tier=2,
    title="Platform Architecture: Observability Pipeline (Loggregator)",
    content="""
# Platform Architecture: Observability Pipeline (Loggregator)

## Pipeline Overview

Logs and metrics from every component follow a serial pipeline:

1. **Loggregator agents** on each CELL collect stdout/stderr from app containers
   and forward envelopes to Doppler.

2. **Doppler** aggregates envelopes from all agents into a buffer. If the buffer
   fills (typically because one or more apps are logging excessively), Doppler
   starts **dropping envelopes**. This is backpressure — it does not crash, it
   silently discards.

3. **Reverse Log Proxy (RLP)** serves the aggregated stream to consumers: log
   drains, the AUTOSCALER, and monitoring tools. If Doppler is dropping, RLP
   consumers see gaps.

4. **AUTOSCALER** reads CPU/memory metrics via the RLP. If the metrics pipeline
   has gaps, the autoscaler may be evaluating stale data — scaling decisions
   during a loggregator backpressure event may be based on outdated load figures.

## Diagnostic Implications

**If metrics appear missing or contradictory:**
Check the METRICS component for doppler buffer warnings before assuming the
metric values are accurate. Log gaps during an incident investigation may be
caused by pipeline saturation, not by the absence of events.

**Doppler buffer thresholds:**
- Normal: <30% utilisation
- Alert: >70% utilisation
- INC-015: reached 98%, triggering envelope drops

**Backpressure is silent:**
Doppler dropping envelopes produces no error in the apps generating the logs.
The only signal is the doppler buffer utilisation metric in the METRICS component.

## Impact on Autoscaler

When the observability pipeline is saturated (INC-015), the AUTOSCALER receives
stale or gapped metrics. This means:
- Scale-up may be delayed (autoscaler doesn't see the load spike)
- Scale-down may be premature (autoscaler sees a false dip after gaps)
- During INC-020, oscillation analysis must account for whether the metrics
  feeding the autoscaler were from the live pipeline or a gapped state.
""",
)


# ============================================================================
# PER-INCIDENT DOCS
# Each incident gets: runbook, error_ref, config
# ============================================================================

# ── INC-001: Wrong port ───────────────────────────────────────────────────────

doc(
    doc_id="runbook_INC-001",
    doc_type="runbook",
    incident_ids=["INC-001"],
    components=["APP", "CELL", "HEALTH", "ROUTER", "SCHEDULER"],
    failure_pattern="wrong_port_binding",
    tier=1,
    title="Runbook: App Listening on Wrong Port",
    content="""
# Runbook: App Listening on Wrong Port

## Symptom
App deploys successfully (container created, process starts) but health checks
immediately fail. ROUTER reports no healthy backends. App never receives traffic.

## Diagnosis Steps

1. **Check HEALTH logs for the failing instance.**
   Look for: `health check failed: connection refused on port 8080`
   This means the app process started but is not accepting connections on the
   expected port.

2. **Check APP logs for the startup message.**
   Look for: `Server starting on port XXXX`
   If this shows a port other than the value of $PORT (typically 8080), the
   app is hardcoding its port.

3. **Confirm $PORT is set correctly.**
   The platform injects $PORT into the container environment. Apps must bind to
   $PORT, not a hardcoded value. Port 3000, 5000, 8000 are common hardcoded ports.

4. **Check CELL logs for container lifecycle.**
   A pattern of: container created → health check failed → container killed →
   new container created (loop) confirms the instance never passes health checks.

5. **Check ROUTER for 503s.**
   If all instances fail health checks, ROUTER has no healthy backends and
   returns 503 to all inbound requests.

## Resolution
Modify the app to bind to `process.env.PORT` (Node.js), `os.environ['PORT']`
(Python), or equivalent. Redeploy.

## Red Herrings
- The build succeeds and the container starts — do not conclude the deploy worked.
- ROUTER 503s appear as a symptom, not the cause.
- The SCHEDULER and CELL logs show normal operation — the failure is in APP.
""",
)

doc(
    doc_id="error_ref_INC-001",
    doc_type="error_ref",
    incident_ids=["INC-001"],
    components=["APP", "HEALTH", "ROUTER"],
    failure_pattern="wrong_port_binding",
    tier=1,
    title="Error Reference: Wrong Port Binding",
    content="""
# Error Reference: Wrong Port Binding

## Log Messages and Their Meanings

### APP: `Server starting on port 3000`
**Meaning:** The app process has started and bound to port 3000.
**Significance:** If the platform expects the app on $PORT (8080), this app will
never receive health check probes and will fail immediately.

### HEALTH: `health check failed: connection refused on port 8080`
**Meaning:** The health checker probed port 8080 (the value of $PORT) and got
ECONNREFUSED. The container is running but the app is not listening on the right port.
**Next step:** Check APP logs for the port the app actually started on.

### HEALTH: `health check timeout after 30s: no response on port 8080`
**Meaning:** Similar to connection refused, but the app may be listening on
another port that doesn't immediately reject connections.

### ROUTER: `503 Service Unavailable: <app>.apps.internal`
**Meaning:** ROUTER has no healthy backends for this app. All instances failed
health checks. This is a consequence of the wrong port, not the cause.

### CELL: `container killed: health check failed after 3 consecutive failures`
**Meaning:** Diego rep has killed the container because it never passed health
checks. Diego will attempt to reschedule the instance.
""",
)

doc(
    doc_id="config_INC-001",
    doc_type="config",
    incident_ids=["INC-001"],
    components=["APP", "CELL", "HEALTH"],
    failure_pattern="wrong_port_binding",
    tier=1,
    title="Configuration Reference: Port Binding and $PORT",
    content="""
# Configuration Reference: Port Binding and $PORT

## $PORT Environment Variable

**What it is:** The platform injects the $PORT environment variable into every
app container. This is the port the platform expects the app to listen on.

**Default value:** 8080 (platform-assigned; do not assume this value in code).

**Rule:** Apps MUST bind to $PORT. Hardcoded ports will always fail health checks.

## Health Check Configuration

| Parameter            | Default | Notes |
|----------------------|---------|-------|
| health check type    | port    | Checks TCP connection to $PORT |
| health check timeout | 30s     | App must bind within this window after startup |
| failure threshold    | 3       | Container killed after 3 consecutive failures |

## Common Mistakes

- `PORT=3000` hardcoded in Procfile or start script — must use `$PORT`
- Docker-style `EXPOSE 8080` in Dockerfile is documentation only — the platform
  assigns $PORT dynamically and it may not be 8080.
- `app.listen(3000)` in Node.js instead of `app.listen(process.env.PORT)`
""",
)

# ── INC-002: Disk quota exhaustion ───────────────────────────────────────────

doc(
    doc_id="runbook_INC-002",
    doc_type="runbook",
    incident_ids=["INC-002"],
    components=["APP", "CELL", "METRICS"],
    failure_pattern="disk_quota_exhaustion",
    tier=1,
    title="Runbook: Container Disk Quota Exhaustion",
    content="""
# Runbook: Container Disk Quota Exhaustion

## Symptom
App instance killed by the platform. CELL logs show `disk_quota_exceeded`.
METRICS shows container disk usage climbing to 100% of quota before the kill.

## Diagnosis Steps

1. **Check METRICS for disk usage trend.**
   Look for: `container_disk_usage` climbing steadily over time.
   A steady climb (not a spike) suggests a write-accumulation problem — logging
   to disk, temporary files not being cleaned, cache growth.

2. **Check APP logs for file writes.**
   Look for: log lines written to files rather than stdout/stderr, temp file
   creation, cache population messages.

3. **Check CELL for the kill event.**
   Look for: `disk_quota_exceeded: container killed` with the instance ID.
   Note the time — correlate with the disk usage trend.

4. **Identify the write pattern.**
   - Steady linear climb → unbounded logging to disk
   - Stepped climb → periodic writes (job output, batch cache)
   - Spike then plateau → one-time large write (build artifact, download)

## Resolution
- Redirect all logging to stdout/stderr (platform captures these, not container disk).
- Implement log rotation if disk logging is required.
- Increase disk quota if the app legitimately needs more disk.
- Add cleanup logic for temporary files.

## Platform Behaviour
Disk quotas are hard limits. The container is killed immediately when the quota
is exceeded — there is no grace period. The instance is rescheduled, but will
hit the same limit again if the root cause is not fixed.
""",
)

doc(
    doc_id="error_ref_INC-002",
    doc_type="error_ref",
    incident_ids=["INC-002"],
    components=["APP", "CELL", "METRICS"],
    failure_pattern="disk_quota_exhaustion",
    tier=1,
    title="Error Reference: Disk Quota Exhaustion",
    content="""
# Error Reference: Disk Quota Exhaustion

## Log Messages and Their Meanings

### METRICS: `container_disk_usage=1.8GB/2GB (90%)`
**Meaning:** The container has used 90% of its 2GB disk quota. At this rate,
the container will be killed when it reaches 100%.
**Next step:** Identify what is writing to disk.

### CELL: `disk_quota_exceeded: container killed — app-XXXX/instance-YYYY`
**Meaning:** The container hit the hard disk quota limit and was immediately
killed by the Diego rep. The platform will reschedule the instance.
**Cause:** Unbounded disk writes — typically logging to files instead of stdout.

### APP: `Writing log to /app/logs/app.log`
**Meaning:** The app is writing logs to a file on the container filesystem,
consuming disk quota. Logs should be written to stdout/stderr instead.

### METRICS: `container_disk_usage=99.9%`
**Meaning:** Disk quota is effectively exhausted. Kill is imminent.
""",
)

doc(
    doc_id="config_INC-002",
    doc_type="config",
    incident_ids=["INC-002"],
    components=["CELL", "APP"],
    failure_pattern="disk_quota_exhaustion",
    tier=1,
    title="Configuration Reference: Container Disk Quota",
    content="""
# Configuration Reference: Container Disk Quota

## Disk Quota Settings

| Parameter        | Default | Configurable |
|------------------|---------|--------------|
| disk_quota       | 1GB     | Yes — cf push -k |
| enforcement      | hard    | No — container killed at 100% |
| cleanup on kill  | yes     | Ephemeral disk cleared on reschedule |

## Key Behaviour
- Disk quota applies to the container ephemeral filesystem only.
- stdout/stderr do NOT consume container disk — they are streamed to Loggregator.
- App source code and droplet are pre-loaded and do not count against the
  runtime disk quota.
- Temporary files in /tmp count against the quota.

## Alert Threshold
Container disk usage >80% of quota is the diagnostic alert threshold.
INC-002 climbed to 99.9% before the disk_quota_exceeded kill.

## Resolution: increase quota
  cf push my-app -k 4G

## Resolution: redirect logging
Replace file writes with stdout in the app:
  - Python: logging.basicConfig(stream=sys.stdout)
  - Node.js: console.log() (writes to stdout by default)
  - Java: configure log4j/logback to use ConsoleAppender
""",
)

# ── INC-003: DNS misconfiguration ────────────────────────────────────────────

doc(
    doc_id="runbook_INC-003",
    doc_type="runbook",
    incident_ids=["INC-003"],
    components=["APP", "CELL", "HEALTH", "CONTROLLER"],
    failure_pattern="dns_misconfiguration",
    tier=2,
    title="Runbook: Service Binding DNS Resolution Failure",
    content="""
# Runbook: Service Binding DNS Resolution Failure

## Symptom
App starts, passes initial health checks, but fails during database connection
phase. APP logs show DNS resolution errors for a service hostname. The service
itself is healthy — only this app cannot reach it.

## Diagnosis Steps

1. **Check APP logs for the connection error.**
   Look for: `getaddrinfo ENOTFOUND <hostname>` or `hostname not resolved: <hostname>`
   Note the exact hostname that failed — compare it to the VCAP_SERVICES binding.

2. **Check CONTROLLER for the service bind operation.**
   Look for: `Service bind: postgresql-prod bound to app-XXXX`
   Verify the bind succeeded. A successful bind injects credentials into VCAP_SERVICES.

3. **Identify the hostname in VCAP_SERVICES.**
   The failed hostname should match the `host` field in the service credentials.
   If the hostname uses an internal DNS suffix (.service.internal, .apps.internal),
   check whether the app's network policy allows DNS resolution to that zone.

4. **Check BUILD_SERVICE for environment variable issues.**
   Some apps bake the DATABASE_URL at build time. If the build ran before the
   service was bound, the URL may be empty or use a placeholder.

5. **Distinguish from network policy issues (INC-010).**
   DNS failure (ENOTFOUND/hostname not resolved) is different from connection
   refused (ECONNREFUSED). DNS failure means the name cannot be resolved;
   ECONNREFUSED means the name resolved but the connection was rejected.

## Resolution
- Re-bind the service to the app: `cf unbind-service`, `cf bind-service`, `cf restart`
- If the hostname uses a custom internal DNS zone, verify DNS entries exist
- If the app bakes DATABASE_URL at build time, trigger a restage after binding

## Red Herrings
- Health checks pass because the health endpoint does not hit the database.
  The app appears healthy but fails on first real request.
""",
)

doc(
    doc_id="error_ref_INC-003",
    doc_type="error_ref",
    incident_ids=["INC-003"],
    components=["APP", "CELL"],
    failure_pattern="dns_misconfiguration",
    tier=2,
    title="Error Reference: DNS Resolution Failure",
    content="""
# Error Reference: DNS Resolution Failure

## Log Messages and Their Meanings

### APP: `getaddrinfo ENOTFOUND postgres-prod.service.internal`
**Meaning:** The app attempted a DNS lookup for the service hostname and the
name does not exist in DNS. The service is bound but its hostname is not
resolvable from inside this app's container.

### APP: `hostname not resolved: redis-cache.internal (NXDOMAIN)`
**Meaning:** DNS query returned NXDOMAIN (no such domain). The internal DNS
zone may not be configured, or the service was moved to a different hostname.

### APP: `Error: connect ECONNREFUSED 10.0.1.5:5432`
**Meaning:** DNS resolved successfully (IP address shown), but the connection
was refused. This is NOT a DNS issue — see INC-010 (network policy) instead.

### CONTROLLER: `Service postgresql-prod bound to app-XXXX — credentials injected`
**Meaning:** The bind succeeded. VCAP_SERVICES now contains the hostname.
If the app still cannot resolve it, the issue is DNS configuration, not binding.
""",
)

doc(
    doc_id="config_INC-003",
    doc_type="config",
    incident_ids=["INC-003"],
    components=["APP", "NETWORK"],
    failure_pattern="dns_misconfiguration",
    tier=2,
    title="Configuration Reference: Service Binding and Internal DNS",
    content="""
# Configuration Reference: Service Binding and Internal DNS

## Service Binding Flow

1. `cf bind-service <app> <service>` triggers SERVICE_BROKER to provision credentials.
2. CONTROLLER injects credentials into the app's VCAP_SERVICES environment variable.
3. App must be restarted (or restaged if build-time env vars are used) to pick up
   the new credentials.

## Internal DNS Zones

| Zone suffix              | Scope                                    |
|--------------------------|------------------------------------------|
| .apps.internal           | App-to-app via internal route (C2C)      |
| .service.internal        | Managed service instances (PostgreSQL, Redis) |
| .svc.cluster.local       | Kubernetes-backed services (if applicable) |

Apps can only resolve hostnames in zones that their network policy allows.
Missing a DNS entry in .service.internal is different from a missing network
policy — check DNS first, then check `cf network-policies`.

## Gotcha: Build-Time Credential Baking
If an app reads DATABASE_URL at build time (e.g. in a settings.py compiled step),
the URL is captured in the droplet before service binding. After binding, the
droplet must be rebuilt:
  cf restage <app>   # triggers restage, picks up new VCAP_SERVICES
""",
)

# ── INC-004: App OOM ──────────────────────────────────────────────────────────

doc(
    doc_id="runbook_INC-004",
    doc_type="runbook",
    incident_ids=["INC-004"],
    components=["APP", "CELL", "METRICS", "SCHEDULER"],
    failure_pattern="app_oom",
    tier=1,
    title="Runbook: App OOM Kill (Memory Quota Exceeded)",
    content="""
# Runbook: App OOM Kill (Memory Quota Exceeded)

## Symptom
App instance repeatedly killed and rescheduled. CELL logs show OOM kill.
METRICS shows memory usage climbing to quota limit. GC warnings appear in APP logs
before the kill.

## Diagnosis Steps

1. **Check METRICS for memory trend.**
   Look for: `container_memory_usage` percentage climbing over time.
   Alert threshold: >85% of quota. Kill occurs at 100%.

2. **Check APP logs for GC activity.**
   Look for: GC warnings, heap size messages, OOM error messages before the kill.
   Frequent GC at high heap usage indicates the app is retaining too much in memory.

3. **Check CELL for the kill event.**
   Look for: `OOM kill: container exceeded memory quota` with instance ID.
   Note whether restarts are cyclic (kill → reschedule → grow → kill = memory leak).

4. **Distinguish app OOM (INC-004) from cell OOM (INC-009).**
   - App OOM: one instance killed, others continue. METRICS shows one container's usage.
   - Cell OOM: ALL instances on a cell killed simultaneously. METRICS shows cell-level memory.

5. **Check for memory leak pattern.**
   If memory usage grows monotonically between restarts, the app has a memory leak.
   If it spikes then holds, the workload may genuinely exceed the quota.

## Resolution
- Increase memory quota: `cf scale <app> -m 1G`
- Fix memory leak in app code
- Reduce per-request memory usage (e.g. streaming instead of loading full datasets)

## Platform Behaviour
App memory quota is a hard limit. The container is killed immediately when exceeded.
Unlike CPU (which throttles), memory over-limit causes an immediate OOM kill.
""",
)

doc(
    doc_id="error_ref_INC-004",
    doc_type="error_ref",
    incident_ids=["INC-004"],
    components=["APP", "CELL", "METRICS"],
    failure_pattern="app_oom",
    tier=1,
    title="Error Reference: App OOM Kill",
    content="""
# Error Reference: App OOM Kill

## Log Messages and Their Meanings

### METRICS: `container_memory_usage=85% (435MB/512MB)`
**Meaning:** App instance is using 85% of its memory quota. This is the alert
threshold — investigate before the instance is killed.

### APP: `GC Warning: heap usage at 92% — forcing major collection`
**Meaning:** The JVM/runtime is under memory pressure and performing aggressive
garbage collection. Memory usage is near the container limit.

### APP: `java.lang.OutOfMemoryError: Java heap space`
**Meaning:** The app ran out of heap memory and threw an OOM error. The container
runtime will kill the container momentarily.

### CELL: `OOM kill: container exceeded memory quota — instance-XXXX killed`
**Meaning:** The Diego rep killed the container because it exceeded its memory
quota. The instance will be rescheduled.

### METRICS: `container_memory_usage=99% — OOM kill imminent`
**Meaning:** Memory is at 99% of quota. Kill is imminent on next allocation.
""",
)

doc(
    doc_id="config_INC-004",
    doc_type="config",
    incident_ids=["INC-004"],
    components=["CELL", "APP"],
    failure_pattern="app_oom",
    tier=1,
    title="Configuration Reference: App Memory Quota",
    content="""
# Configuration Reference: App Memory Quota

## Memory Quota Settings

| Parameter             | Default | Configurable |
|-----------------------|---------|--------------|
| memory quota          | 256MB   | Yes — cf push -m or cf scale -m |
| enforcement           | hard    | No — OOM kill at 100% |
| swap                  | disabled| No swap in containers |

## Key Behaviour
- Memory quota applies to the total RSS of the container, including the app
  process, its runtime (JVM, Node.js, Python), and any side processes.
- OOM kill is immediate — no grace period, no SIGTERM before SIGKILL.
- Unlike disk (which gives a WARN log before kill), memory kills happen without
  a platform warning; the METRICS alert threshold is the only advance notice.

## Alert Threshold
Container memory >85% of quota warrants investigation.
INC-004: 85% with GC warnings, OOM killed at 99%.

## Resize command
  cf scale my-app -m 1G     # increase to 1GB
  cf scale my-app -m 512M   # shrink to 512MB
""",
)

# ── INC-005: TLS cert expiry (service binding) ────────────────────────────────

doc(
    doc_id="runbook_INC-005",
    doc_type="runbook",
    incident_ids=["INC-005"],
    components=["APP", "CONTROLLER", "HEALTH", "METRICS", "ROUTER"],
    failure_pattern="certificate_expiry",
    tier=2,
    title="Runbook: TLS Certificate Expiry (Service Binding)",
    content="""
# Runbook: TLS Certificate Expiry (Service Binding)

## Symptom
App was operating normally. At a specific moment, all connections to a bound
service (RabbitMQ, Redis, PostgreSQL) fail simultaneously with TLS errors.
Health checks may or may not fail depending on whether the health endpoint uses
the affected connection. CERT WARNING log entries appear in logs hours before failure.

## Diagnosis Steps

1. **Search for CERT WARNING entries before the failure.**
   Look for: `CERT WARNING: cert expires in Xh Ym`
   Note the certificate path and the predicted expiry time.

2. **Identify the exact failure time.**
   Look for: `TLS handshake error: certificate has expired` in APP logs.
   The failure will be abrupt — compare the timestamp with the predicted expiry.

3. **Identify the scope of affected connections.**
   All connections using the same certificate fail simultaneously.
   If only RabbitMQ connections fail, the RabbitMQ binding cert expired.
   If all internal service connections fail, an internal CA cert may have expired.

4. **Distinguish from INC-025 (mTLS platform cert).**
   INC-005: A specific service binding cert expired — only connections to that
   service are affected.
   INC-025: The platform internal mTLS cert expired — all inter-service
   communication on the platform fails.

5. **Check CONTROLLER for cert rotation options.**
   Look for: `Cert rotated` messages — if rotation is available, it will appear here.

## Resolution
- Rotate the service binding certificate via the Service Broker.
- Rebind the service to the app: `cf unbind-service && cf bind-service && cf restart`
- For platform certs (INC-025): rotate via BOSH.

## Prevention
Set monitoring alerts on CERT WARNING log patterns. The warning appears hours
to days before expiry — there is always time to rotate before impact.
""",
)

doc(
    doc_id="error_ref_INC-005",
    doc_type="error_ref",
    incident_ids=["INC-005", "INC-025"],
    components=["APP", "CONTROLLER", "ROUTER", "HEALTH"],
    failure_pattern="certificate_expiry",
    tier=2,
    title="Error Reference: TLS Certificate Expiry",
    content="""
# Error Reference: TLS Certificate Expiry

## Log Messages and Their Meanings

### CONTROLLER: `CERT WARNING: internal mTLS cert expires in 6h12m: /certs/internal-ca.crt`
**Meaning:** A platform certificate is approaching expiry. This is an advance
warning — the cert has not yet expired and connections still work.
**Action required:** Rotate the certificate before the deadline or expect
complete connection failure at expiry time.

### ROUTER: `CERT WARNING: router mTLS cert expires in 6h11m`
**Meaning:** The ROUTER's mTLS certificate is also near expiry. Typically
co-expires with the platform CA cert.

### APP: `TLS handshake error: certificate has expired`
**Meaning:** A TLS connection failed because the server's certificate is past
its validity period. This appears after the expiry timestamp, not before.

### APP: `Error: certificate has expired or is not yet valid`
**Meaning:** Same as above. The connection is rejected at the TLS layer.

### ROUTER: `mTLS verification failed: all backend connections rejected`
**Meaning:** ROUTER cannot establish mTLS connections to any backend. This
indicates a platform-level cert expiry (INC-025), not a single service binding.

### HEALTH: `Health check failed: TLS cert expired on health endpoint`
**Meaning:** The health check endpoint uses TLS, and that TLS cert has expired.
The instance will be marked unhealthy even though the app process is running.

### CONTROLLER: `Cert rotated via BOSH: new cert valid until YYYY-MM-DD`
**Meaning:** Certificate rotation completed successfully. New connections will
use the rotated cert.
""",
)

doc(
    doc_id="config_INC-005",
    doc_type="config",
    incident_ids=["INC-005", "INC-025"],
    components=["CONTROLLER", "ROUTER", "APP"],
    failure_pattern="certificate_expiry",
    tier=2,
    title="Configuration Reference: TLS Certificates",
    content="""
# Configuration Reference: TLS Certificates

## Certificate Types

| Type                 | Scope                            | Rotation Method |
|----------------------|----------------------------------|-----------------|
| Service binding cert | One service ↔ one app            | cf unbind + cf bind |
| Internal mTLS cert   | All platform inter-service comms | BOSH cert rotation |
| Router TLS cert      | Inbound HTTPS                    | BOSH cert rotation |

## Expiry Behaviour
- Certificate expiry is a hard cliff: 100% success → 100% failure at the expiry second.
- No degraded state: the connection either succeeds or fails entirely.
- CERT WARNING log entries appear hours before expiry (timing varies by component).

## Monitoring
The CERT WARNING log pattern is the only advance signal. Configure log-based
alerting on:
  pattern: "CERT WARNING"
  condition: expires in < 48h
  action: page on-call

## Key Timing (INC-025)
- Warning logged: 6h12m before expiry
- 5-hour gap of routine DEBUG logs
- Certificate expires: abrupt failure of all internal connections
- Time gap between warning and failure does NOT mean the warning is unrelated.
""",
)

# ── INC-006: Buildpack cache corruption ───────────────────────────────────────

doc(
    doc_id="runbook_INC-006",
    doc_type="runbook",
    incident_ids=["INC-006"],
    components=["BUILD_SERVICE", "CONTROLLER"],
    failure_pattern="buildpack_cache_corruption",
    tier=1,
    title="Runbook: Buildpack Cache Corruption",
    content="""
# Runbook: Buildpack Cache Corruption

## Symptom
App that was previously deploying successfully begins failing at the BUILD_SERVICE
stage. The build fails with dependency install errors even though the package
manifest (package.json, requirements.txt) has not changed. The error often
references cached modules that are broken or incompatible.

## Diagnosis Steps

1. **Check BUILD_SERVICE for install errors.**
   Look for: npm install, pip install, or bundle install errors.
   Note whether the error mentions a cached version or a specific cached path.

2. **Check if the manifest changed.**
   If package.json/requirements.txt is unchanged but install fails, the cache
   is likely corrupt — not the manifest.

3. **Look for cache-specific error messages.**
   Look for: `using cached node_modules`, `cache hit`, followed by import or
   symbol errors. A corrupted cache passes the install step but fails at runtime.

4. **Check CONTROLLER for the build failure.**
   Look for: `BUILD_SERVICE stage failed` — this confirms the failure is in
   staging, not at runtime.

5. **Distinguish from INC-011 (buildpack version mismatch).**
   INC-006: Cache is corrupt — the install step itself fails or installs broken deps.
   INC-011: Build succeeds but the installed version is wrong — runtime ImportError.

## Resolution
Clear the buildpack cache and retry:
  cf delete-buildpack-cache <app>  # if available via plugin
  cf push <app> --no-startup       # trigger fresh build without cache

## Red Herrings
- The package manifest is unchanged — do not chase dependency version issues
  until the cache is cleared.
""",
)

doc(
    doc_id="error_ref_INC-006",
    doc_type="error_ref",
    incident_ids=["INC-006"],
    components=["BUILD_SERVICE"],
    failure_pattern="buildpack_cache_corruption",
    tier=1,
    title="Error Reference: Buildpack Cache Corruption",
    content="""
# Error Reference: Buildpack Cache Corruption

## Log Messages and Their Meanings

### BUILD_SERVICE: `Cache hit: using cached node_modules from previous build`
**Meaning:** The buildpack found a cached dependency directory and will use it
instead of running a fresh install. If the cache is corrupt, the build will
proceed but with broken modules.

### BUILD_SERVICE: `npm ERR! Cannot find module 'webpack/lib/optimize'`
**Meaning:** A required module is missing from node_modules. Combined with a
cache hit message, this means the cached copy is incomplete or corrupt.

### BUILD_SERVICE: `pip install ERROR: cached wheel is invalid: hash mismatch`
**Meaning:** The pip cache contains a wheel file with a checksum that doesn't
match the expected hash. Cache corruption is the likely cause.

### BUILD_SERVICE: `stage failed: dependency install error`
**Meaning:** The build pipeline failed during the dependency install phase.
The droplet was not created. CONTROLLER will report the push as failed.

### CONTROLLER: `BUILD_SERVICE stage failed for app-XXXX — push aborted`
**Meaning:** The app push failed during staging. The existing running version
(if any) continues to serve traffic — the failed push does not affect running instances.
""",
)

doc(
    doc_id="config_INC-006",
    doc_type="config",
    incident_ids=["INC-006"],
    components=["BUILD_SERVICE"],
    failure_pattern="buildpack_cache_corruption",
    tier=1,
    title="Configuration Reference: Buildpack Cache",
    content="""
# Configuration Reference: Buildpack Cache

## Buildpack Cache Behaviour
- The platform caches buildpack output (installed dependencies) between deploys
  to speed up staging. The cache key is app GUID + buildpack combination.
- Cache is stored in BLOB_STORE alongside droplets.
- Cache corruption can occur after interrupted builds or storage errors.

## Cache Control Options

| Option                  | Effect |
|-------------------------|--------|
| cf push --no-startup    | Triggers restage; does NOT clear cache |
| cf restage <app>        | Triggers restage; does NOT clear cache |
| Buildpack cache delete  | Requires cf CLI plugin or operator access |

## When to Clear Cache
- After a failed build that cannot be explained by manifest changes.
- After a BUILD_SERVICE storage error or interrupted build.
- After upgrading the buildpack major version.

## Cache vs. Registry Rate Limits (INC-012)
If the build fails with HTTP 429 or `rate limit exceeded` from an external
registry (Docker Hub), this is NOT a cache issue — see INC-012 configuration.
""",
)

# ── INC-007: Missing route mapping ──────────────────────────────────────────

doc(
    doc_id="runbook_INC-007",
    doc_type="runbook",
    incident_ids=["INC-007"],
    components=["CONTROLLER", "ROUTER"],
    failure_pattern="missing_route_mapping",
    tier=1,
    title="Runbook: App Deployed Without Route Mapping",
    content="""
# Runbook: App Deployed Without Route Mapping

## Symptom
App is running and healthy. HEALTH checks pass. But all inbound HTTP requests
return 502. ROUTER logs show no backends for the app's hostname.

## Diagnosis Steps

1. **Check ROUTER for backend existence.**
   Look for: `502 Bad Gateway: no route to <hostname>` or
   `no backends found for <app>.cfapps.io`
   This means ROUTER has no entry for this hostname — the route was never mapped.

2. **Check CONTROLLER for route registration.**
   Look for: `Route registered: <hostname> → app-XXXX`
   If this message is absent, the route was never mapped via `cf map-route`.

3. **Distinguish 502 from 503.**
   502 = ROUTER found no route entry for this hostname (INC-007 pattern).
   503 = ROUTER has the route but no healthy backends (health check failures,
   INC-001/INC-009 pattern).

4. **Check MESSAGE_BUS for route registration events.**
   Routes propagate via NATS. If MESSAGE_BUS is saturated (INC-016), route
   registrations may not have propagated even if `cf map-route` was run.

## Resolution
  cf map-route <app> <domain> --hostname <hostname>
  # e.g. cf map-route my-app cfapps.io --hostname payments-api

## Red Herrings
- The app is running and healthy — do not investigate CELL or HEALTH.
- The 502 from ROUTER looks like a backend problem. It is a routing table
  configuration problem — no backend was ever registered.
""",
)

doc(
    doc_id="error_ref_INC-007",
    doc_type="error_ref",
    incident_ids=["INC-007"],
    components=["ROUTER", "CONTROLLER"],
    failure_pattern="missing_route_mapping",
    tier=1,
    title="Error Reference: Missing Route Mapping (502)",
    content="""
# Error Reference: Missing Route Mapping (502)

## Log Messages and Their Meanings

### ROUTER: `502 Bad Gateway: no route to payments-api.cfapps.io`
**Meaning:** ROUTER received a request for this hostname but has no routing
table entry for it. The app exists but was never mapped to this hostname.
**Cause:** `cf map-route` was not run after deployment.

### ROUTER: `no backends found for hostname: payments-api.cfapps.io`
**Meaning:** The routing table has no entry for this hostname at all.
Distinct from 503 (which means backends exist but are all unhealthy).

### CONTROLLER: (absence of) `Route registered: payments-api.cfapps.io → app-XXXX`
**Significance:** If you do NOT see this message in CONTROLLER logs after
deployment, the route was never mapped. The 502 is expected.

## 502 vs 503 Reference

| Error | Meaning                                    | Likely Cause |
|-------|-------------------------------------------|--------------|
| 502   | No route entry for this hostname           | cf map-route not run (INC-007) OR stale cache after route swap (INC-018) |
| 503   | Route exists but no healthy backends       | All instances failing health checks (INC-001, INC-009) |
""",
)

doc(
    doc_id="config_INC-007",
    doc_type="config",
    incident_ids=["INC-007"],
    components=["ROUTER", "CONTROLLER"],
    failure_pattern="missing_route_mapping",
    tier=1,
    title="Configuration Reference: Route Mapping",
    content="""
# Configuration Reference: Route Mapping

## Route Mapping Basics
An app must be explicitly mapped to a hostname before it receives traffic.
Deploying an app (`cf push`) does not automatically create an external route
unless a `routes:` block is included in the manifest.

## Manifest Route Configuration

  ---
  applications:
  - name: payments-api
    routes:
    - route: payments-api.cfapps.io
    - route: payments-api.apps.internal  # internal C2C route

## Manual Route Mapping
  cf map-route <app> <domain> --hostname <hostname>
  cf map-route payments-api cfapps.io --hostname payments-api
  cf map-route payments-api apps.internal --hostname payments-api

## Route Propagation
Routes registered via NATS propagate to all ROUTER instances within ~30s
(Gorouter cache TTL). Immediately after mapping, some router instances may
not yet have the route in cache.

## Common Mistakes
- Forgetting `routes:` in manifest → app starts but gets no traffic.
- Using wrong domain (cfapps.io vs apps.internal) → route registered but
  DNS does not resolve from the expected caller.
""",
)

# ── INC-008: Connection pool exhaustion ─────────────────────────────────────

doc(
    doc_id="runbook_INC-008",
    doc_type="runbook",
    incident_ids=["INC-008"],
    components=["APP", "METRICS", "ROUTER", "HEALTH", "SCHEDULER"],
    failure_pattern="connection_pool_exhaustion",
    tier=2,
    title="Runbook: Database Connection Pool Exhaustion",
    content="""
# Runbook: Database Connection Pool Exhaustion

## Symptom
503s from ROUTER, increasing over time. METRICS shows DB pool utilisation
climbing steadily. APP logs show TimeoutErrors. Autoscaler may fire scale-out,
but 503s continue after new instances start.

## Diagnosis Steps

1. **Check METRICS for pool utilisation trend.**
   Look for: `db_pool_active_connections=X/20` values climbing over time.
   Alert threshold: >85% sustained (17/20). Crisis: 100% (20/20 POOL EXHAUSTED).

2. **Identify slow queries holding connections.**
   Look for: `Slow query detected: SELECT ... (2340ms)` in APP logs.
   Connections held by slow queries are not returned to the pool until the query
   completes. Multiple slow queries can exhaust the pool quickly.

3. **Check for the scale-out trap.**
   If SCHEDULER shows `Scale-out triggered` and new instances hit
   `pool limit immediately; pool still exhausted` — scaling is not the fix.
   The bottleneck is the shared database pool, not per-instance compute.

4. **Determine if pool exhaustion is sustained or transient.**
   Transient: pool hits 100% briefly then recovers → slow query spike.
   Sustained: pool stays at 100% for minutes → ongoing slow query problem or
   too-small pool for the workload.

5. **Check ROUTER for 503 onset time.**
   503s begin at the moment the pool hits 100%. This correlation confirms
   pool exhaustion as the cause of 503s.

## Resolution
- Short-term: identify and kill long-running queries in the database.
- Medium-term: add query timeouts in the app (statement_timeout in PostgreSQL).
- Long-term: increase pool size or optimize the slow queries.

## Red Herrings
- Autoscaler scale-out looks like it might help — new instances hit the same
  exhausted pool and provide zero relief.
- HEALTH check failures appear after pool exhaustion — they are a consequence,
  not the cause.
""",
)

doc(
    doc_id="error_ref_INC-008",
    doc_type="error_ref",
    incident_ids=["INC-008"],
    components=["APP", "METRICS", "ROUTER"],
    failure_pattern="connection_pool_exhaustion",
    tier=2,
    title="Error Reference: Connection Pool Exhaustion",
    content="""
# Error Reference: Connection Pool Exhaustion

## Log Messages and Their Meanings

### METRICS: `db_pool_active_connections=12/20`
**Meaning:** Pool at 60% — normal operation. No action needed.

### METRICS: `db_pool_active_connections=18/20`
**Meaning:** Pool at 90% — above alert threshold. Investigate for slow queries.

### METRICS: `db_pool_active_connections=20/20 (POOL EXHAUSTED)`
**Meaning:** Pool is fully exhausted. All 20 connections are held (likely by slow
queries). New database requests will immediately timeout or queue.

### APP: `Slow query detected: SELECT * FROM orders WHERE... (2340ms)`
**Meaning:** A query took 2340ms — significantly above normal. This connection
is held for the duration of the query.

### APP: `TimeoutError: connection pool exhausted after 5000ms`
**Meaning:** The app attempted to acquire a database connection from the pool
and waited 5000ms with no connection becoming available. The request fails.

### ROUTER: `503 Service Unavailable: payments-api.apps.internal`
**Meaning:** Backend returned an error (the app returned 500/503 due to
TimeoutError). This is a consequence of pool exhaustion.

### APP: `New instance hit pool limit immediately; pool still exhausted`
**Meaning:** A newly scaled-out instance tried to acquire a connection and
immediately hit the exhausted pool. Scaling provided zero relief.
""",
)

doc(
    doc_id="config_INC-008",
    doc_type="config",
    incident_ids=["INC-008"],
    components=["APP"],
    failure_pattern="connection_pool_exhaustion",
    tier=2,
    title="Configuration Reference: Database Connection Pool",
    content="""
# Configuration Reference: Database Connection Pool

## Pool Configuration Parameters

| Parameter             | Typical Default | INC-008 Value |
|-----------------------|-----------------|---------------|
| pool max connections  | 10              | 20            |
| connection timeout    | 5000ms          | 5000ms        |
| statement timeout     | none            | not set (cause of issue) |
| idle connection timeout | 600s          | default       |

## Alert Thresholds
- db_pool_active_connections >85% → investigate for slow queries
- db_pool_active_connections 100% (POOL EXHAUSTED) → connections will timeout

## Key Insight: Pool Exhaustion vs. Instance Count
Pool exhaustion is a shared-resource problem. All instances of an app share the
same database connection pool (or the same database has a fixed max connections).
Scaling the app horizontally adds more consumers competing for the same pool,
making exhaustion worse, not better.

## Resolution: Add Statement Timeout (PostgreSQL)
In app configuration or via database URL parameter:
  DATABASE_URL=postgres://host/db?statement_timeout=3000
This forces slow queries to fail fast, returning connections to the pool.

## Resolution: Increase Pool Size
Only effective if the database server can handle more connections:
  DB_POOL_SIZE=50   (in app environment variables)
""",
)

# ── INC-009: Cell OOM ─────────────────────────────────────────────────────────

doc(
    doc_id="runbook_INC-009",
    doc_type="runbook",
    incident_ids=["INC-009"],
    components=["CELL", "METRICS", "SCHEDULER", "HEALTH", "ROUTER"],
    failure_pattern="cell_oom",
    tier=2,
    title="Runbook: Cell-Level OOM (All Co-Located Instances Killed)",
    content="""
# Runbook: Cell-Level OOM (All Co-Located Instances Killed)

## Symptom
Multiple app instances crash simultaneously. METRICS shows a specific cell's
memory dropping to near-zero. SCHEDULER begins emergency rescheduling of all
instances from that cell. Remaining cells experience elevated load.

## Diagnosis Steps

1. **Confirm simultaneous crash pattern.**
   Look for multiple `container killed` events from the SAME cell node_id,
   within milliseconds of each other. Simultaneous kills = cell failure.
   (Staggered kills = app bug or separate health check failures.)

2. **Check METRICS for the cell-level memory signal.**
   Look for: `cell_memory_available=512MB` on a cell with 64GB total.
   This is a cell-level metric (not per-container). Near-zero available means
   the cell itself ran out of memory.

3. **Identify which cell failed.**
   The CELL `node_id` field in kill events identifies the failed cell.
   All kills will share the same `node_id`.

4. **Check SCHEDULER for rescheduling cascade.**
   Look for: `Rescheduling N instances from cell-XXX`
   If N instances are rescheduled onto already-busy cells, secondary OOM events
   may follow (rescheduling pressure).

5. **Distinguish from app-level OOM (INC-004).**
   Cell OOM: multiple instances on same cell killed simultaneously.
   App OOM: one instance killed, others on same cell continue normally.

6. **Check HEALTH and ROUTER for consequence.**
   Health check timeouts and 503s appear AFTER the cell OOM. They are
   consequences, not causes. Do not investigate HEALTH as root cause.

## Resolution
- Immediate: let SCHEDULER reschedule to remaining cells.
- If secondary cell pressure: scale the platform (add cells) or reduce instance counts.
- Root cause: a single app instance on the cell consumed excess memory, triggering
  cell-level OOM. Identify which app was growing and add per-app memory limits.
""",
)

doc(
    doc_id="error_ref_INC-009",
    doc_type="error_ref",
    incident_ids=["INC-009"],
    components=["CELL", "METRICS", "SCHEDULER"],
    failure_pattern="cell_oom",
    tier=2,
    title="Error Reference: Cell-Level OOM Kill",
    content="""
# Error Reference: Cell-Level OOM Kill

## Log Messages and Their Meanings

### METRICS: `cell_memory_available=512MB (cell-009, total: 64GB)`
**Meaning:** Cell cell-009 has only 512MB of its 64GB available — 99.2% consumed.
This is a cell-level metric, not per-app. The cell itself is out of memory.

### CELL: `OOM kill: container exceeded memory on cell cell-009 — instance-0000 killed`
### CELL: `OOM kill: container exceeded memory on cell cell-009 — instance-0001 killed`
### CELL: `OOM kill: container exceeded memory on cell cell-009 — instance-0002 killed`
**Meaning (all three):** Multiple instances on the same cell are killed in rapid
succession. The common `cell-009` node_id and near-simultaneous timestamps
confirm this is a cell-level failure, not individual app OOMs.

### SCHEDULER: `Rescheduling 8 instances from cell-009 (OOM event)`
**Meaning:** Diego detected the cell failure and is moving all 8 instances to
other cells. If remaining cells have limited capacity, this may trigger
secondary OOM events.

### HEALTH: `Health check timeout: instance-0003 (cell-009) — no response in 5000ms`
**Meaning:** This health check failure is a CONSEQUENCE of the cell OOM, not an
independent problem. The instance was killed before it could respond.

### ROUTER: `503: all backends unhealthy for payments-api`
**Meaning:** All instances of this app were on cell-009 and were killed
simultaneously. ROUTER has no healthy backends. This is a consequence.
""",
)

doc(
    doc_id="config_INC-009",
    doc_type="config",
    incident_ids=["INC-009"],
    components=["CELL", "METRICS"],
    failure_pattern="cell_oom",
    tier=2,
    title="Configuration Reference: Cell Memory and Placement",
    content="""
# Configuration Reference: Cell Memory and Placement

## Cell Memory Thresholds

| Metric                  | Normal       | Alert Threshold | INC-009 Value |
|-------------------------|--------------|-----------------|---------------|
| cell_memory_available   | >20% of cell | <10% of cell    | 512MB of 64GB (0.8%) |

## Cell vs. App Memory
- **App memory quota** limits one container's usage (cf scale -m).
- **Cell memory** is the total physical memory of the host VM.
- A cell hosts many containers. If the sum of container usage exceeds cell
  total, the Linux kernel OOM killer fires — killing processes on the cell.
- The cell OOM is a platform-level event, not an individual app limit.

## Placement Strategy
- Diego places instances across cells based on available capacity.
- If all instances of an app land on one cell (small deployments), a cell OOM
  kills all instances simultaneously.
- Run multiple instances and use placement constraints to spread across cells:
  cf scale <app> -i 3   # three instances, Diego will spread across cells

## Key Distinction
OOM kill source:
- `CELL: OOM kill: container exceeded memory quota` → **app-level** OOM (INC-004)
- `METRICS: cell_memory_available=0` + multiple simultaneous kills → **cell-level** OOM (INC-009)
""",
)

# ── INC-010: Missing network policy ──────────────────────────────────────────

doc(
    doc_id="runbook_INC-010",
    doc_type="runbook",
    incident_ids=["INC-010"],
    components=["APP", "NETWORK", "ROUTER", "CONTROLLER"],
    failure_pattern="missing_network_policy",
    tier=2,
    title="Runbook: Missing Network Policy (App-to-App Communication Blocked)",
    content="""
# Runbook: Missing Network Policy (App-to-App Communication Blocked)

## Symptom
App A can be reached externally. App A cannot reach App B internally.
APP logs on the calling side show ECONNREFUSED. NETWORK logs on the
receiving cell show DENY events. Both apps are healthy — the issue is
network policy enforcement.

## Diagnosis Steps

1. **Check APP logs for the error type.**
   Look for: `connect ECONNREFUSED <IP>:<port>` or
   `Error: connect ECONNREFUSED inventory-service.apps.internal`
   ECONNREFUSED from an internal hostname means the packet was silently
   dropped by the network layer — it looks identical to the destination app
   being down, but it is a policy enforcement event.

2. **Check NETWORK logs for DENY events.**
   Look for: `DENY: source=order-service dest=inventory-service port=8080`
   DENY is logged on the RECEIVING cell's NETWORK component, not the sender.
   This is the definitive confirmation that policy is missing.

3. **Verify the apps are running.**
   Check CELL and HEALTH for both apps. If both are healthy, the issue is
   not app availability — it is network policy.

4. **Check CONTROLLER for policy registration.**
   Look for (absence of): `Network policy added: order-service → inventory-service`
   If this message is missing, `cf add-network-policy` was never run.

5. **Distinguish from DNS failure (INC-003).**
   DNS failure: `getaddrinfo ENOTFOUND <hostname>` — name cannot be resolved.
   Network policy block: `connect ECONNREFUSED <IP>:<port>` — name resolved,
   connection dropped.

## Resolution
  cf add-network-policy order-service --destination-app inventory-service \\
    --protocol tcp --port 8080

## Key Insight
The platform uses deny-by-default between apps. No app can make a network
connection to another app without an explicit policy. After a namespace migration
or re-deployment, policies may need to be re-applied.
""",
)

doc(
    doc_id="error_ref_INC-010",
    doc_type="error_ref",
    incident_ids=["INC-010"],
    components=["APP", "NETWORK"],
    failure_pattern="missing_network_policy",
    tier=2,
    title="Error Reference: Missing Network Policy",
    content="""
# Error Reference: Missing Network Policy

## Log Messages and Their Meanings

### APP: `connect ECONNREFUSED inventory-service.apps.internal:8080`
**Meaning:** The app resolved the hostname to an IP address but the connection
was refused. In most cases this means the destination app is down — but when
NETWORK DENY events co-occur on the receiving cell, it means the packet was
silently dropped by the Silk CNI enforcer.
**Next step:** Check NETWORK logs on the receiving cell for DENY events.

### NETWORK: `DENY: source=order-service dest=inventory-service port=8080 proto=tcp`
**Meaning:** The platform's network enforcement layer (Silk CNI) dropped the
connection attempt because no policy exists allowing order-service to connect
to inventory-service on port 8080. This is logged on the RECEIVING cell.

### CONTROLLER: `Network policy added: order-service → inventory-service port=8080`
**Meaning:** A `cf add-network-policy` command succeeded. Traffic between these
apps on this port is now allowed.

## ECONNREFUSED: Two Different Causes

| Source of ECONNREFUSED | Root Cause | How to Confirm |
|------------------------|-----------|----------------|
| App is down (crashed, OOM) | App failure | Check CELL/HEALTH — app not running |
| Network policy missing | Policy enforcement | Check NETWORK — DENY event present |

Always check NETWORK logs before concluding the destination app is the problem.
""",
)

doc(
    doc_id="config_INC-010",
    doc_type="config",
    incident_ids=["INC-010"],
    components=["NETWORK", "CONTROLLER"],
    failure_pattern="missing_network_policy",
    tier=2,
    title="Configuration Reference: Network Policies",
    content="""
# Configuration Reference: Network Policies (Silk CNI)

## Default Posture
The platform uses **deny-all** between apps by default. No inter-app network
traffic is allowed without an explicit policy.

## Policy Management

  # View existing policies
  cf network-policies

  # Add a policy
  cf add-network-policy <source-app> --destination-app <dest-app> \\
    --protocol tcp --port <port>

  # Remove a policy
  cf remove-network-policy <source-app> --destination-app <dest-app> \\
    --protocol tcp --port <port>

## Policy Scope
- Policies are per app GUID, not per hostname.
- After a deployment that creates a new app GUID (e.g. blue-green deploy),
  policies on the old GUID do not transfer to the new GUID.
- After namespace migrations, policies may need to be recreated.

## Policy Logging
DENY events are logged by the NETWORK component on the **receiving** cell,
not on the sending app's cell. When investigating ECONNREFUSED, check NETWORK
logs on the destination app's node_id.

## Internal Hostnames
- `.apps.internal` routes are for C2C (container-to-container) traffic.
- These require BOTH a network policy AND an internal route mapping.
- `cf map-route <app> apps.internal --hostname <hostname>` creates the internal route.
- `cf add-network-policy` grants the network-level access.
""",
)

# ── INC-011: Buildpack version mismatch ──────────────────────────────────────

doc(
    doc_id="runbook_INC-011",
    doc_type="runbook",
    incident_ids=["INC-011"],
    components=["APP", "BUILD_SERVICE", "CELL", "HEALTH"],
    failure_pattern="buildpack_version_mismatch",
    tier=1,
    title="Runbook: Buildpack Dependency Version Mismatch",
    content="""
# Runbook: Buildpack Dependency Version Mismatch

## Symptom
Build succeeds. Container starts. App crashes immediately during startup with
an ImportError, ModuleNotFoundError, or NoSuchMethodError. The error references
a specific module or class that exists in the required version but not in the
installed version.

## Diagnosis Steps

1. **Check APP logs for the error at startup.**
   Look for: `ImportError: cannot import name 'X' from 'numpy'`
   or `ModuleNotFoundError: No module named 'numpy.core._multiarray_umath'`
   Note the module name and which import or method is missing.

2. **Check BUILD_SERVICE for the installed version.**
   Look for: `Installing numpy==1.21.0 (buildpack default)`
   If the app requires 1.24 but the buildpack installs 1.21, there is a version
   mismatch.

3. **Distinguish from INC-006 (cache corruption).**
   INC-006: Build fails during install phase.
   INC-011: Build succeeds, runtime crashes with missing symbol/import.

4. **Check if requirements.txt pins the version.**
   If requirements.txt specifies `numpy>=1.24` but the buildpack ignores this
   and installs its bundled version, the mismatch is a buildpack limitation.

5. **Check HEALTH for restart loop.**
   Look for: repeated container start → immediate crash → reschedule cycles.
   This confirms the app consistently fails at startup, not randomly.

## Resolution
- Pin the exact version in requirements.txt: `numpy==1.24.0`
- Use a vendor buildpack or custom buildpack that supports the required version.
- Include a `.python-version` file to pin the Python runtime version.

## Red Herrings
- BUILD_SERVICE shows SUCCESS — do not conclude the app will work at runtime.
- The error appears at runtime, not at build time.
""",
)

doc(
    doc_id="error_ref_INC-011",
    doc_type="error_ref",
    incident_ids=["INC-011"],
    components=["APP", "BUILD_SERVICE"],
    failure_pattern="buildpack_version_mismatch",
    tier=1,
    title="Error Reference: Buildpack Version Mismatch",
    content="""
# Error Reference: Buildpack Version Mismatch

## Log Messages and Their Meanings

### BUILD_SERVICE: `Installing numpy==1.21.0 (buildpack default, overrides pinned 1.24)`
**Meaning:** The buildpack's bundled version of numpy (1.21.0) was installed
instead of the version pinned in requirements.txt. The build succeeds but
the runtime will be incompatible.

### BUILD_SERVICE: `Stage complete: droplet uploaded successfully`
**Meaning:** The build succeeded and the droplet was created. This message is
NOT a guarantee that the app will run — version mismatches surface at runtime.

### APP: `ImportError: cannot import name 'AxisError' from 'numpy' (numpy v1.21.0)`
**Meaning:** The app tried to import a symbol that exists in numpy 1.24 but not
in numpy 1.21. The version mismatch installed by the buildpack is the cause.

### CELL: `Container started for instance-0000`
**Meaning:** Container started successfully. The crash happens after container
start, during app process initialization.

### HEALTH: `Health check failed: instance-0000 exited with code 1`
**Meaning:** The app process exited immediately (exit code 1 = error) after
container start. Combined with the ImportError in APP logs, this confirms
startup crash due to import failure.
""",
)

doc(
    doc_id="config_INC-011",
    doc_type="config",
    incident_ids=["INC-011"],
    components=["BUILD_SERVICE"],
    failure_pattern="buildpack_version_mismatch",
    tier=1,
    title="Configuration Reference: Buildpack Dependency Pinning",
    content="""
# Configuration Reference: Buildpack Dependency Pinning

## How Buildpack Dependency Resolution Works
1. Buildpack reads the app manifest (requirements.txt, package.json, etc.)
2. For each dependency, buildpack checks if it has a bundled version.
3. If the buildpack ships a bundled version, it installs that — potentially
   ignoring the pinned version in the manifest.
4. If no bundled version exists, the buildpack fetches from the registry
   (subject to rate limits — INC-012).

## Python Buildpack Version Pinning

  requirements.txt:
    numpy==1.24.0      # exact pin — buildpack may override
    numpy>=1.24,<2.0   # range pin — buildpack may still install 1.21

  .python-version:
    3.11.4             # pins the Python runtime version

## Node.js Buildpack Version Pinning

  package.json engines field:
    "engines": { "node": ">=18.0.0 <20.0.0" }

  .nvmrc:
    18.17.0            # exact Node version pin

## Buildpack Lifecycle
Build success ≠ runtime success. Always test runtime behaviour after
deploying a new buildpack version or changing dependency pins.
Version mismatches between buildpack defaults and app requirements are
a common silent failure mode.
""",
)

# ── INC-012: Docker Hub rate limit ────────────────────────────────────────────

doc(
    doc_id="runbook_INC-012",
    doc_type="runbook",
    incident_ids=["INC-012"],
    components=["BUILD_SERVICE", "CONTROLLER"],
    failure_pattern="registry_rate_limit",
    tier=1,
    title="Runbook: Container Registry Pull Rate Limit",
    content="""
# Runbook: Container Registry Pull Rate Limit (Docker Hub)

## Symptom
Build failures with HTTP 429 errors. Multiple parallel deploys from the same
time window all fail at the BUILD_SERVICE stage. BUILD_SERVICE logs show rate
limit errors from registry.hub.docker.com.

## Diagnosis Steps

1. **Check BUILD_SERVICE for HTTP 429 errors.**
   Look for: `HTTP 429 Too Many Requests` from `registry.hub.docker.com`
   or `rate limit exceeded: anonymous pull from 1.2.3.4 (60 pulls in 6hr)`

2. **Check the time window.**
   Docker Hub rate limits anonymous pulls to 100/6hr per IP. If multiple builds
   ran in a short window, the shared egress IP may have hit the limit.

3. **Check CONTROLLER for failed pushes.**
   All push failures during the rate-limited window will show BUILD_SERVICE failures.
   The failure pattern is time-correlated, not app-specific.

4. **Distinguish from INC-006 (cache corruption) and INC-011 (version mismatch).**
   Rate limit: HTTP 429, affects all concurrent deploys from shared IP.
   Cache corruption: fails during install phase, one app at a time.
   Version mismatch: build succeeds, runtime fails.

## Resolution
- Short-term: wait for the 6-hour rate limit window to reset, then redeploy.
- Medium-term: authenticate Docker Hub pulls with a Docker Hub account (200 pulls/6hr).
- Long-term: use an internal registry mirror that proxies Docker Hub pulls.

## Platform Configuration
Docker Hub anonymous pull limit: 100 pulls per 6 hours per IP.
The platform uses shared egress IPs for outbound traffic from build workers.
High-volume deploy windows can exhaust the limit quickly.
""",
)

doc(
    doc_id="error_ref_INC-012",
    doc_type="error_ref",
    incident_ids=["INC-012"],
    components=["BUILD_SERVICE"],
    failure_pattern="registry_rate_limit",
    tier=1,
    title="Error Reference: Container Registry Rate Limit",
    content="""
# Error Reference: Container Registry Rate Limit

## Log Messages and Their Meanings

### BUILD_SERVICE: `HTTP 429 Too Many Requests: registry.hub.docker.com`
**Meaning:** Docker Hub rejected the pull request because the rate limit for
this IP address has been exceeded. The build cannot proceed.

### BUILD_SERVICE: `rate limit exceeded: 100 anonymous pulls in 6hr from 10.0.0.1`
**Meaning:** The platform's shared egress IP has exceeded Docker Hub's anonymous
pull rate limit (100 pulls/6hr). All builds using this egress IP are blocked.

### BUILD_SERVICE: `stage failed: base image pull rejected (rate limited)`
**Meaning:** The buildpack tried to pull a base image from Docker Hub and was
rate-limited. The droplet was not created.

### CONTROLLER: `BUILD_SERVICE stage failed for app-XXXX — push aborted`
**Meaning:** The app push failed during staging due to the registry rate limit.
This will affect all apps deploying in the same time window.
""",
)

doc(
    doc_id="config_INC-012",
    doc_type="config",
    incident_ids=["INC-012"],
    components=["BUILD_SERVICE"],
    failure_pattern="registry_rate_limit",
    tier=1,
    title="Configuration Reference: Container Registry Pull Authentication",
    content="""
# Configuration Reference: Container Registry Pull Authentication

## Docker Hub Rate Limits

| Auth Type      | Rate Limit             |
|----------------|------------------------|
| Anonymous      | 100 pulls / 6hr / IP   |
| Authenticated  | 200 pulls / 6hr / account |
| Docker Pro/Team| Unlimited              |

## Platform Registry Configuration
Build workers share egress IPs. High-volume deploy windows (e.g. 60+ concurrent
deploys) can exhaust anonymous pull limits across all builds from that IP.

## Configuring Registry Authentication

  # Set Docker Hub credentials in build worker environment:
  CF_DOCKER_PASSWORD=<token>
  CF_DOCKER_USERNAME=<username>

  # Or configure in platform operations manifest:
  properties:
    buildpacks:
      registry_auth:
        username: <username>
        password: <token>

## Internal Registry Mirror
For production environments: configure an internal registry mirror to proxy
Docker Hub. Pulls from the internal mirror are not subject to Docker Hub limits.
""",
)

# ── INC-013: Service broker timeout ──────────────────────────────────────────

doc(
    doc_id="runbook_INC-013",
    doc_type="runbook",
    incident_ids=["INC-013"],
    components=["CONTROLLER", "SERVICE_BROKER", "METRICS"],
    failure_pattern="service_broker_timeout",
    tier=2,
    title="Runbook: Service Broker Timeout Blocking Deployment",
    content="""
# Runbook: Service Broker Timeout Blocking Deployment

## Symptom
Deployments hang then fail with timeout errors. CONTROLLER logs show repeated
timeout waiting for SERVICE_BROKER. Running apps are unaffected — this is a
control plane failure. Only new deploys are blocked.

## Diagnosis Steps

1. **Check CONTROLLER for broker timeout.**
   Look for: `Timeout waiting for service broker: redis-broker (30s)`
   or `SERVICE_BROKER bind request timed out for app-XXXX`

2. **Identify which broker is overloaded.**
   The broker name in the timeout message identifies the affected service type
   (Redis, RabbitMQ, PostgreSQL, etc.)

3. **Check METRICS for broker queue depth.**
   Look for: `service_broker_queue_depth` growing, or response time metrics
   on the broker's own health endpoint.

4. **Verify running apps are healthy.**
   SERVICE_BROKER timeout affects only NEW binds — apps already running with
   bound credentials continue normally. Check ROUTER and HEALTH for running apps.

5. **Determine if the broker is overloaded or unreachable.**
   Overloaded: broker responds slowly (>30s). Usually concurrent bind requests.
   Unreachable: broker returns connection refused or times out at TCP level.

## Resolution
- Scale the service broker if it is overloaded.
- Stagger deployments to reduce concurrent bind requests.
- Implement a circuit breaker on the broker if it is repeatedly timing out.

## Platform Behaviour
CONTROLLER waits synchronously for the SERVICE_BROKER bind response (default 30s).
If the broker times out, the entire deployment pipeline is blocked — the app
cannot start without its service credentials injected via VCAP_SERVICES.
""",
)

doc(
    doc_id="error_ref_INC-013",
    doc_type="error_ref",
    incident_ids=["INC-013"],
    components=["CONTROLLER", "SERVICE_BROKER"],
    failure_pattern="service_broker_timeout",
    tier=2,
    title="Error Reference: Service Broker Timeout",
    content="""
# Error Reference: Service Broker Timeout

## Log Messages and Their Meanings

### CONTROLLER: `Timeout waiting for service broker: redis-broker (30s timeout)`
**Meaning:** CONTROLLER sent a bind request to the Redis service broker and
waited 30 seconds without a response. The bind (and the entire deploy) is blocked.

### CONTROLLER: `SERVICE_BROKER bind request failed: connection timeout`
**Meaning:** The broker is unreachable or too slow. The app will not receive
VCAP_SERVICES credentials. Deploy is aborted.

### SERVICE_BROKER: `Bind request queue depth: 47 pending`
**Meaning:** The broker has 47 bind requests queued. Concurrent deploys are
overwhelming the broker's capacity. Timeouts are expected.

### METRICS: `service_broker_response_time_ms=28450`
**Meaning:** The broker is taking 28.4 seconds to respond — just under the
30-second timeout. Any additional latency will cause timeouts.

### CONTROLLER: `Bind succeeded: redis-broker returned credentials for app-XXXX`
**Meaning:** After the backpressure cleared, the bind eventually succeeded.
The deploy can proceed.
""",
)

doc(
    doc_id="config_INC-013",
    doc_type="config",
    incident_ids=["INC-013"],
    components=["SERVICE_BROKER", "CONTROLLER"],
    failure_pattern="service_broker_timeout",
    tier=2,
    title="Configuration Reference: Service Broker Timeout",
    content="""
# Configuration Reference: Service Broker Timeout

## CONTROLLER Broker Timeout

| Parameter              | Default | Notes |
|------------------------|---------|-------|
| broker_client_timeout  | 30s     | CONTROLLER waits this long for broker response |
| bind_async_timeout     | 60s     | For async bind operations |

## Service Broker Capacity
Service brokers handle bind/unbind requests synchronously by default.
Under high concurrent deploy load, broker queues can grow beyond the CONTROLLER
timeout window.

## Diagnosis Query
Check for concurrent bind requests in CONTROLLER logs:
  SELECT timestamp, message FROM logs
  WHERE component='CONTROLLER'
  AND message LIKE '%broker%'
  ORDER BY timestamp

## Mitigation
- Increase broker instance count to handle concurrent bind requests.
- Implement broker-side async bind (Open Service Broker API async flow).
- Stagger deployments during peak hours.
- Increase CONTROLLER `broker_client_timeout` if broker is slow but reliable.
""",
)

# ── INC-014: Autoscaler during rolling deploy ─────────────────────────────────

doc(
    doc_id="runbook_INC-014",
    doc_type="runbook",
    incident_ids=["INC-014"],
    components=["APP", "AUTOSCALER", "CONTROLLER", "ROUTER", "SCHEDULER", "CELL"],
    failure_pattern="autoscaler_rolling_deploy_interference",
    tier=3,
    title="Runbook: Autoscaler Interference During Rolling Deploy",
    content="""
# Runbook: Autoscaler Interference During Rolling Deploy

## Symptom
During a rolling deploy, approximately 50% of requests fail. Some succeed (v2
instances) and some fail (v1 instances with incompatible request format). The
autoscaler fired a scale-up event at the start of the deploy, creating a mix
of v1 and v2 instances that persists longer than expected.

## Diagnosis Steps

1. **Check AUTOSCALER for scale events during deploy window.**
   Look for: `Scale-up triggered: N→M instances` with a timestamp during the
   rolling deploy. If the scale-up happened at the beginning of the deploy, the
   new instances started from the OLD version (v1).

2. **Check CELL for which image version each instance is running.**
   Look for: instance start events — old instances show the v1 droplet,
   new instances from the rolling deploy show v2.

3. **Check APP for version-specific error patterns.**
   If 50% of requests fail with format/schema errors and 50% succeed,
   the request routing is hitting a mix of v1 and v2 instances.

4. **Check SCHEDULER for instance count during deploy.**
   Rolling deploy + scale-up = more total instances than expected. SCHEDULER
   logs will show instance counts above the target during the transition.

5. **Determine if the errors are transient or sustained.**
   A normal rolling deploy completes in minutes. If errors persist >10 minutes,
   the deploy may be stalled or the autoscaler may be interfering repeatedly.

## Resolution
- Pause the autoscaler during rolling deploys.
- Ensure v1 and v2 are backward-compatible (use API versioning).
- Configure autoscaler min/max to prevent scale events during deploy windows.

## Red Herrings
- 50% failure rate looks like a ROUTER load-balancing bug. It is a version
  incompatibility between co-existing v1 and v2 instances.
""",
)

doc(
    doc_id="error_ref_INC-014",
    doc_type="error_ref",
    incident_ids=["INC-014"],
    components=["APP", "AUTOSCALER", "ROUTER"],
    failure_pattern="autoscaler_rolling_deploy_interference",
    tier=3,
    title="Error Reference: Autoscaler/Rolling Deploy Version Mix",
    content="""
# Error Reference: Autoscaler/Rolling Deploy Version Mix

## Log Messages and Their Meanings

### AUTOSCALER: `Scale-up triggered: payments-api 2→4 instances (CPU threshold exceeded)`
**Meaning:** The autoscaler added 2 instances at the start of the deploy. These
new instances use the current (v1) droplet — not the v2 being deployed. This
creates a larger pool of v1 instances that the rolling deploy must replace.

### ROUTER: `200 OK: payments-api (instance-0000)` then `400 Bad Request: payments-api (instance-0002)`
**Meaning:** Requests to different instances return different results. instance-0000
is v2, instance-0002 is v1. The request format differs between versions.

### APP: `v2: processing new request format (JSON v2 schema)`
### APP: `v1: unrecognized field 'correlation_id' in request body`
**Meaning:** Two different versions of the app are handling requests simultaneously.
The v1 instance doesn't understand the v2 request format.

### SCHEDULER: `Rolling deploy in progress: 2/6 instances updated to v2`
**Meaning:** 2 of the 6 total instances (the autoscaler added 2 extra) are on v2.
4 are still on v1. ROUTER distributes traffic across all 6.
""",
)

doc(
    doc_id="config_INC-014",
    doc_type="config",
    incident_ids=["INC-014"],
    components=["AUTOSCALER", "SCHEDULER"],
    failure_pattern="autoscaler_rolling_deploy_interference",
    tier=3,
    title="Configuration Reference: Autoscaler and Rolling Deploy Interaction",
    content="""
# Configuration Reference: Autoscaler and Rolling Deploy Interaction

## The Interaction Problem
Rolling deploys replace instances one-at-a-time. Autoscaler adds instances
using the CURRENT droplet (old version). If autoscaler fires at the start of
a rolling deploy, it creates additional v1 instances that the rolling deploy
must then also replace — extending the mixed-version window.

## Autoscaler Configuration

  # Pause autoscaler during deploy (cf CLI):
  cf update-autoscaling-limits <app> <min> <max> --disable

  # Or configure deploy-time suspend in app manifest (if supported):
  autoscaling:
    enabled: false
    during_deploy: true

## Rolling Deploy Configuration

  # Manifest: control rolling deploy behaviour
  ---
  applications:
  - name: payments-api
    strategy: rolling
    rolling_deploy:
      max_in_flight: 1         # replace one instance at a time
      wait_for_health: true    # wait for health check before next instance

## Backward Compatibility
If autoscaler interference is unavoidable, ensure v1 and v2 are backward
compatible — v1 instances must be able to handle v2 requests (or vice versa)
without errors. Use API versioning headers rather than breaking schema changes.
""",
)

# ── INC-015: Loggregator saturation ──────────────────────────────────────────

doc(
    doc_id="runbook_INC-015",
    doc_type="runbook",
    incident_ids=["INC-015"],
    components=["METRICS", "ROUTER"],
    failure_pattern="loggregator_saturation",
    tier=2,
    title="Runbook: Loggregator Backpressure and Metric Drops",
    content="""
# Runbook: Loggregator Backpressure and Metric Drops

## Symptom
Log gaps appear in monitoring dashboards. Metrics stop updating. Autoscaler
may make incorrect scaling decisions based on stale data. METRICS logs show
Doppler buffer at high utilisation. No app errors are present — the apps
themselves are healthy.

## Diagnosis Steps

1. **Check METRICS for Doppler buffer utilisation.**
   Look for: `doppler_buffer_utilization=98%` or `envelope_drop_count=XXXX`
   A buffer above 70% warrants investigation. At 98%, significant drops are occurring.

2. **Identify the high-volume log source.**
   Look for: `high_log_volume_source: app-XXXX (15000 lines/min)`
   One app logging excessively can saturate the entire pipeline.

3. **Check ROUTER for any traffic anomalies.**
   If the AUTOSCALER is making decisions from stale metrics, ROUTER may show
   unexpected traffic distribution (too few or too many instances active).

4. **Assess impact on AUTOSCALER.**
   If Doppler is dropping metrics and the AUTOSCALER reads via RLP, scaling
   decisions during the saturation window may be based on old data.

5. **Distinguish from NATS saturation (INC-016).**
   INC-015: Logging pipeline saturated — apps appear healthy, logs/metrics drop.
   INC-016: Message bus saturated — routing table stale, cell heartbeats lost.

## Resolution
- Identify and rate-limit the high-volume logging app.
- Reduce log verbosity (change log level from DEBUG to INFO/WARN).
- Increase Doppler buffer capacity (platform operator action).

## Key Behaviour
Doppler drops envelopes silently — no error in the apps producing logs.
The only signal is the Doppler buffer utilisation metric.
""",
)

doc(
    doc_id="error_ref_INC-015",
    doc_type="error_ref",
    incident_ids=["INC-015"],
    components=["METRICS"],
    failure_pattern="loggregator_saturation",
    tier=2,
    title="Error Reference: Loggregator Saturation",
    content="""
# Error Reference: Loggregator Saturation

## Log Messages and Their Meanings

### METRICS: `doppler_buffer_utilization=98%`
**Meaning:** The Doppler aggregation buffer is at 98% capacity. Envelopes
(log lines and metrics) are being dropped. Consumers of the RLP (autoscaler,
log drains, monitoring) will see gaps.

### METRICS: `envelope_drop_count=4521 (last 60s)`
**Meaning:** 4521 log/metric envelopes were dropped in the last 60 seconds
because the Doppler buffer was full. This is a silent drop — no error is
logged in the source apps.

### METRICS: `high_log_volume_source: app-XXXX (15000 lines/min)`
**Meaning:** One app is emitting 15,000 log lines per minute — likely the
cause of Doppler buffer saturation.

### ROUTER: (no error messages — ROUTER is unaffected by logging saturation)
**Meaning:** ROUTER operates independently of the logging pipeline. Traffic
continues normally even when logs are dropping.
""",
)

doc(
    doc_id="config_INC-015",
    doc_type="config",
    incident_ids=["INC-015"],
    components=["METRICS"],
    failure_pattern="loggregator_saturation",
    tier=2,
    title="Configuration Reference: Loggregator Doppler Buffer",
    content="""
# Configuration Reference: Loggregator Doppler Buffer

## Doppler Buffer Thresholds

| Metric                    | Normal | Alert  | INC-015 Value |
|---------------------------|--------|--------|---------------|
| doppler_buffer_utilization | <30%  | >70%   | 98%           |

## Pipeline Capacity Limits
The Loggregator pipeline is capacity-constrained at the Doppler buffer.
When the buffer fills, Doppler drops envelopes rather than applying backpressure
to agents. Source apps never know their logs are being dropped.

## Log Verbosity Controls
Reduce logging volume at the source:

  # Set log level in app environment:
  LOG_LEVEL=warn   # suppress INFO/DEBUG logs

  # Or via cf set-env:
  cf set-env <app> LOG_LEVEL warn
  cf restart <app>

## Doppler Buffer Configuration (operator)
Increase Doppler buffer capacity in BOSH deployment manifest:
  doppler:
    buffer_size: 10000   # default: 1000 envelopes

## Impact on Autoscaler
When Doppler drops metrics, the AUTOSCALER reads stale data via RLP.
Scaling decisions during saturation events should be reviewed manually.
""",
)

# ── INC-016: NATS saturation ─────────────────────────────────────────────────

doc(
    doc_id="runbook_INC-016",
    doc_type="runbook",
    incident_ids=["INC-016"],
    components=["MESSAGE_BUS", "ROUTER", "SCHEDULER", "METRICS"],
    failure_pattern="nats_saturation",
    tier=3,
    title="Runbook: NATS Message Bus Saturation",
    content="""
# Runbook: NATS Message Bus Saturation

## Symptom
Multiple downstream components appear to fail simultaneously: routing table
becomes stale, cell heartbeats go missing, Diego begins evacuating healthy cells.
None of these components show errors in their own internal logs. The common cause
is MESSAGE_BUS saturation.

## Diagnosis Steps

1. **Check MESSAGE_BUS for message rate spike.**
   Look for: `NATS: message rate=48000/s (normal: 2000/s)`
   A rate 20x+ normal indicates a runaway publisher.

2. **Check MESSAGE_BUS for slow consumer drops.**
   Look for: `NATS: slow consumer detected on subscription router.register`
   followed by: `NATS: dropping slow consumer router.register`
   When NATS drops a subscriber, that subscriber stops receiving messages.

3. **Identify the runaway publisher.**
   Look for: `NATS: identified publisher: metrics-agent/cell-011 — 49800 msg/s`
   The publisher identity tells you which component to restart.

4. **Confirm downstream symptoms are consequences, not causes.**
   - ROUTER: routing table stale → consequence (not receiving NATS updates).
   - SCHEDULER: cell heartbeat missing → consequence (not receiving NATS heartbeats).
   - CELL evacuation → consequence (SCHEDULER marked cell as failed due to missed heartbeats).
   The cell being evacuated is healthy — it was never the problem.

5. **Check for recovery after publisher restart.**
   Look for: `NATS: rate normalizing after metrics-agent restart: 1800/s`
   followed by: `ROUTER: routing table refreshed`, `SCHEDULER: cell heartbeat restored`

## Resolution
- Identify and restart the runaway publisher.
- If the publisher is a metrics-agent with a bug, the platform operator must
  patch and redeploy it.

## Red Herrings
- Cell evacuation looks like a cell failure — the cell is healthy.
- ROUTER routing table staleness looks like a ROUTER bug — it is NATS dropping ROUTER.
""",
)

doc(
    doc_id="error_ref_INC-016",
    doc_type="error_ref",
    incident_ids=["INC-016"],
    components=["MESSAGE_BUS", "ROUTER", "SCHEDULER"],
    failure_pattern="nats_saturation",
    tier=3,
    title="Error Reference: NATS Message Bus Saturation",
    content="""
# Error Reference: NATS Message Bus Saturation

## Log Messages and Their Meanings

### MESSAGE_BUS: `NATS: message rate=48000/s (normal: 2000/s)`
**Meaning:** NATS is processing 48,000 messages/second — 24x the normal 2,000/s.
A runaway publisher is flooding the bus. Downstream subscribers will fall behind.

### MESSAGE_BUS: `NATS: slow consumer detected on subscription router.register`
**Meaning:** The gorouter subscriber for route registrations cannot keep up.
NATS will drop it if it falls further behind.

### MESSAGE_BUS: `NATS: dropping slow consumer router.register; queue overflow`
**Meaning:** NATS has dropped the gorouter subscriber. ROUTER will no longer
receive route registration or deregistration messages. Routing table will become stale.

### ROUTER: `NATS subscription lag: routing table not updated for 8s`
**Meaning:** ROUTER has not received a NATS message for 8 seconds — consistent
with being a dropped slow consumer.

### SCHEDULER: `Cell heartbeat not received for cell-003 in 10s`
**Meaning:** The cell heartbeat (published via NATS) is not arriving at the scheduler.
This is because NATS dropped the scheduler's subscription, not because the cell failed.

### SCHEDULER: `Evacuating LRPs from cell-003 (precautionary)`
**Meaning:** After missing heartbeats, SCHEDULER is moving instances off cell-003
as a precaution. Cell-003 is healthy — this evacuation is unnecessary and caused
by NATS dropping the scheduler's subscription.

### MESSAGE_BUS: `NATS: identified publisher: metrics-agent/cell-011 — 49800 msg/s`
**Meaning:** The runaway publisher has been identified. Restarting this component
should restore normal message rates.
""",
)

doc(
    doc_id="config_INC-016",
    doc_type="config",
    incident_ids=["INC-016"],
    components=["MESSAGE_BUS"],
    failure_pattern="nats_saturation",
    tier=3,
    title="Configuration Reference: NATS Message Bus",
    content="""
# Configuration Reference: NATS Message Bus (NATS)

## NATS Behaviour
- NATS is memory-buffered: when a subscriber cannot keep up with message rate,
  NATS drops the subscriber rather than blocking the publisher.
- This means saturation is **asymmetric**: the publisher continues normally
  while subscribers silently stop receiving messages.
- NATS does NOT crash under saturation — it selectively drops slow consumers.

## Message Rate Thresholds

| Metric          | Normal    | Alert Threshold | INC-016 Value |
|-----------------|-----------|-----------------|---------------|
| NATS message rate | ~2,000/s | >10,000/s      | 48,000/s      |

## Subscriptions That Lose Messages Under Saturation
- `router.register` — gorouter route registrations (routing table staleness)
- `router.greet` — gorouter greeting/handshake
- `hm9000.heartbeat` — Diego cell heartbeats (false cell failures)
- `doppler.firehose` — metrics pipeline

## Monitoring
Configure alerts on NATS message rate >10,000/s:
  pattern: NATS: message rate
  threshold: 10000
  action: alert on-call

## Publisher Identification
When saturation occurs, NATS logs the identified publisher once the rate exceeds
a configured threshold. Look for `NATS: identified publisher:` in MESSAGE_BUS logs.
""",
)

# ── INC-017: Noisy neighbor CPU ───────────────────────────────────────────────

doc(
    doc_id="runbook_INC-017",
    doc_type="runbook",
    incident_ids=["INC-017"],
    components=["APP", "HEALTH", "METRICS", "SCHEDULER"],
    failure_pattern="noisy_neighbor_cpu",
    tier=2,
    title="Runbook: Noisy Neighbor CPU Throttling",
    content="""
# Runbook: Noisy Neighbor CPU Throttling

## Symptom
Health checks begin timing out on a specific cell. The apps on that cell are
running but responding slowly. METRICS shows one app consuming 95% of cell CPU.
Other apps on the same cell are throttled as a consequence.

## Diagnosis Steps

1. **Check METRICS for cell-level CPU.**
   Look for: `cell_cpu_utilization=94% (cell-012)`
   If one cell is at 94% CPU sustained, co-located apps are being throttled.

2. **Identify the CPU-consuming app.**
   Look for: `app_cpu_usage=95% (app-XXXX on cell-012)`
   This is the noisy neighbor — one app consuming nearly all CPU on a shared cell.

3. **Check HEALTH for timeout pattern.**
   Look for: `health check timeout: 4100ms (threshold: 5000ms)` on instances
   on cell-012. The slow health check response is due to CPU throttling, not app logic.
   Note: threshold is 5000ms; INC-017 reached 4100ms — close to failing.

4. **Confirm the cell-specific pattern.**
   All timing issues should be confined to instances on cell-012.
   Instances of the same app on other cells should respond normally.

5. **Distinguish from INC-009 (cell OOM).**
   Cell OOM: instances killed. Cell CPU exhaustion: instances throttled (slow, not dead).

## Resolution
- Short-term: reschedule the noisy neighbor app to a dedicated cell.
- Medium-term: add CPU limits to the noisy neighbor app (`cf scale -k` or CF quota).
- Platform: ensure CPU-intensive apps are isolated from latency-sensitive apps.

## Platform Behaviour
CPU is NOT a hard limit. A CPU-heavy container throttles co-located containers
but does NOT kill them. This is the key difference from memory (hard kill).
""",
)

doc(
    doc_id="error_ref_INC-017",
    doc_type="error_ref",
    incident_ids=["INC-017"],
    components=["APP", "HEALTH", "METRICS"],
    failure_pattern="noisy_neighbor_cpu",
    tier=2,
    title="Error Reference: Noisy Neighbor CPU Throttling",
    content="""
# Error Reference: Noisy Neighbor CPU Throttling

## Log Messages and Their Meanings

### METRICS: `cell_cpu_utilization=94% (cell-012)`
**Meaning:** Cell-012 is at 94% CPU — above the 85% alert threshold. Co-located
apps are being CPU-throttled. This is the signal that identifies the noisy neighbor situation.

### METRICS: `app_cpu_usage=95% (app-analytics on cell-012)`
**Meaning:** One app is consuming 95% of cell-012's CPU. This is the noisy neighbor.
Other apps on cell-012 are left with ~5% of CPU capacity.

### HEALTH: `health check response: 4100ms (threshold: 5000ms) on cell-012`
**Meaning:** The health check response is 4100ms — 4x slower than normal (<1000ms).
The app is not crashed — it is CPU-throttled. The 5000ms timeout has not been
reached yet, but this is a warning signal.

### APP: `request latency: 8200ms (normal: 200ms) — cell-012`
**Meaning:** Request latency on this cell is 41x higher than normal due to CPU
throttling. Instances on other cells show normal 200ms latency.

### SCHEDULER: `Rescheduling app-analytics from cell-012 (CPU exhaustion)`
**Meaning:** The SCHEDULER is moving the noisy neighbor to a less-loaded cell.
After rescheduling, other apps on cell-012 should recover to normal CPU levels.
""",
)

doc(
    doc_id="config_INC-017",
    doc_type="config",
    incident_ids=["INC-017"],
    components=["CELL", "METRICS"],
    failure_pattern="noisy_neighbor_cpu",
    tier=2,
    title="Configuration Reference: Cell CPU Limits",
    content="""
# Configuration Reference: Cell CPU and Container Throttling

## CPU vs Memory: Key Difference

| Resource | Enforcement | Over-limit behaviour |
|----------|-------------|---------------------|
| Memory   | Hard limit  | Container killed immediately (OOM) |
| CPU      | Soft limit  | Container throttled — still runs, but slowly |

## CPU Alert Thresholds

| Metric                  | Normal | Alert Threshold | INC-017 Value |
|-------------------------|--------|-----------------|---------------|
| cell_cpu_utilization    | <70%   | >85% sustained  | 94%           |
| health check response   | <1000ms| >3000ms         | 4100ms        |

## Container CPU Allocation
The platform uses Linux cgroups CPU shares. By default, containers on the same
cell share CPU proportionally. One container consuming 95% leaves 5% for all others.

## CPU-Based App Isolation
- CPU-intensive apps (batch jobs, analytics, ML inference) should run on
  dedicated cells or org/space quotas with CPU limits.
- `cf scale` does not expose CPU limits directly in all CF versions.
  Use org/space quotas or BOSH deployment isolation segments.

## Identifying Noisy Neighbors
Check METRICS for `app_cpu_usage` on the affected cell. The app with the
highest CPU percentage on that cell is the noisy neighbor.
""",
)

# ── INC-018: Route cache staleness ───────────────────────────────────────────

doc(
    doc_id="runbook_INC-018",
    doc_type="runbook",
    incident_ids=["INC-018"],
    components=["ROUTER", "CONTROLLER", "CELL"],
    failure_pattern="route_cache_staleness",
    tier=2,
    title="Runbook: Gorouter Route Cache Staleness After Blue-Green Swap",
    content="""
# Runbook: Gorouter Route Cache Staleness After Blue-Green Swap

## Symptom
After a blue-green route swap, 502 errors appear for approximately 30-45 seconds.
ROUTER logs show connections to the deregistered (old/blue) app. The new (green)
app is healthy and registered, but some ROUTER instances are still using cached
routes to the old app.

## Diagnosis Steps

1. **Check ROUTER for 502s with timing.**
   Look for: `502 Bad Gateway: backend connection refused` on the swapped route.
   Note the start time relative to the route swap. If 502s begin at swap time
   and resolve ~30s later, this is cache TTL staleness.

2. **Check CONTROLLER for the route swap sequence.**
   Look for: `Route deregistered: payments-api → app-blue-XXXX`
   followed by: `Route registered: payments-api → app-green-XXXX`
   The deregistration message propagates via NATS, but each ROUTER instance
   caches entries for ~30s.

3. **Confirm the green app is healthy.**
   Check HEALTH and CELL for the green app — it should be running and passing
   health checks. The 502s are not caused by the green app.

4. **Distinguish from INC-007 (missing route mapping).**
   INC-007: 502 because the route was NEVER registered — persists indefinitely.
   INC-018: 502 because the route is STILL pointing to the old app — resolves after TTL.

5. **Verify recovery after ~30s.**
   If 502s resolve on their own within 30-45 seconds of the swap, cache staleness
   is confirmed. If 502s persist longer, investigate whether the green app is actually healthy.

## Resolution
- This is expected platform behaviour — the TTL is ~30s by default.
- For zero-downtime deploys: use the `--no-route` flag during blue app deregistration
  and add a 30-second wait before routing to green.
- For critical routes: reduce the Gorouter cache TTL (platform operator action).

## Red Herrings
- 502s look like the green app has a problem. The green app is fine.
- The timing resolves on its own — do not roll back the deploy unless 502s persist >60s.
""",
)

doc(
    doc_id="error_ref_INC-018",
    doc_type="error_ref",
    incident_ids=["INC-018"],
    components=["ROUTER", "CONTROLLER"],
    failure_pattern="route_cache_staleness",
    tier=2,
    title="Error Reference: Route Cache Staleness (502s After Blue-Green)",
    content="""
# Error Reference: Route Cache Staleness (502s After Blue-Green Swap)

## Log Messages and Their Meanings

### ROUTER: `502 Bad Gateway: backend connection refused — payments-api → 10.0.1.5:8080`
**Meaning:** ROUTER selected a backend (10.0.1.5:8080 = the old/blue app instance)
that is no longer running. The connection was refused because the container was
already stopped. The cached route entry is stale.

### CONTROLLER: `Route deregistered: payments-api.cfapps.io → app-blue-XXXX`
**Meaning:** The old blue app's route was deregistered. This propagates via NATS
to all ROUTER instances, but each instance will use its cache for up to 30s.

### CONTROLLER: `Route registered: payments-api.cfapps.io → app-green-XXXX`
**Meaning:** The new green app's route is registered. New connections after cache
expiry will go to the green app.

### ROUTER: `Route cache expired for payments-api.cfapps.io — refreshing from NATS`
**Meaning:** The 30-second cache TTL expired. ROUTER now uses the current NATS
state and routes to the green app. 502s will stop appearing.

## 502 Timing Reference

| Time after swap | Expected behaviour |
|-----------------|-------------------|
| 0-30s           | ~30% of requests hit stale cache (502) |
| 30-45s          | Cache TTL expires; all ROUTER instances update |
| >45s            | All 502s should resolve. If not, investigate green app. |
""",
)

doc(
    doc_id="config_INC-018",
    doc_type="config",
    incident_ids=["INC-018"],
    components=["ROUTER"],
    failure_pattern="route_cache_staleness",
    tier=2,
    title="Configuration Reference: Gorouter Cache TTL",
    content="""
# Configuration Reference: Gorouter Route Cache TTL

## Cache TTL

| Parameter             | Default   | Notes |
|-----------------------|-----------|-------|
| route cache TTL       | ~30s      | Fixed; configurable by platform operator |
| deregistration lag    | up to 30s | Time for deregistration to propagate to all router instances |

## Blue-Green Deploy Timing
To achieve zero-downtime blue-green deploys with no 502 window:

  1. Deploy green app (do not map routes yet)
  2. Verify green app health checks pass
  3. Map the route to green: cf map-route green-app ...
  4. Wait 30 seconds (allow cache to propagate)
  5. Unmap the route from blue: cf unmap-route blue-app ...
  6. Blue still in routing table for up to 30s after unmap
  7. Wait another 30 seconds before stopping blue

## Reducing the Cache TTL
Platform operators can reduce the default TTL in the Gorouter configuration:
  router:
    route_service_timeout: 60s
    drain_wait: 20s
    # Cache TTL is not directly configurable in all CF versions;
    # route registration interval controls effective TTL.

## Key Insight
INC-018: Stale cache caused 502s for 45s after route swap.
Gorouter cache TTL (~30s) is the fixed value — this is expected behaviour,
not a bug. Plan blue-green swap procedures to account for the TTL window.
""",
)

# ── INC-019: Circular startup dependency ─────────────────────────────────────

doc(
    doc_id="runbook_INC-019",
    doc_type="runbook",
    incident_ids=["INC-019"],
    components=["APP", "CELL", "HEALTH", "SCHEDULER"],
    failure_pattern="circular_startup_dependency",
    tier=3,
    title="Runbook: Circular Startup Dependency Deadlock",
    content="""
# Runbook: Circular Startup Dependency Deadlock

## Symptom
Two services are being deployed. Both start, both fail health checks, both
are rescheduled, both fail again — in a loop. Neither service ever passes
health checks. Logs show each service waiting for the other to be healthy.
Instance restart loops without convergence.

## Diagnosis Steps

1. **Check APP logs for startup dependency checks.**
   Look for: `Waiting for inventory-service to be healthy before starting`
   on Service A, AND: `Waiting for order-service to be healthy before starting`
   on Service B. If both are waiting for the other, a circular dependency exists.

2. **Check HEALTH for both services.**
   Both services will show health check failures because neither fully starts.
   The HEALTH logs alone look like two independent failures — the correlation
   comes from APP startup messages.

3. **Check SCHEDULER for restart patterns.**
   Both services should show repeated reschedule cycles. The cycle period is
   the health check timeout interval.

4. **Map the dependency graph.**
   Draw the startup dependency graph. If A→B AND B→A (or any cycle), deadlock
   is the diagnosis.

5. **Distinguish from INC-001 (wrong port) and INC-003 (DNS failure).**
   INC-001: One service fails, the other is fine.
   INC-003: DNS resolution fails; no explicit "waiting for X" message.
   INC-019: Both services fail health checks with explicit waits for each other.

## Resolution
- Break the circular dependency: determine which service can start without the other.
- Implement lazy initialization: services should start and pass health checks
  independently, then establish connections to dependencies once running.
- Use health check endpoints that report healthy before dependency connections
  are fully established.

## Red Herrings
- Each individual service looks like a normal startup failure — the deadlock
  is only visible when both services' logs are examined together.
""",
)

doc(
    doc_id="error_ref_INC-019",
    doc_type="error_ref",
    incident_ids=["INC-019"],
    components=["APP", "HEALTH"],
    failure_pattern="circular_startup_dependency",
    tier=3,
    title="Error Reference: Circular Startup Dependency",
    content="""
# Error Reference: Circular Startup Dependency

## Log Messages and Their Meanings

### APP (order-service): `Waiting for inventory-service to be healthy before starting`
**Meaning:** order-service's startup code is polling inventory-service's health
endpoint and will not complete initialization until it receives a healthy response.
If inventory-service is also waiting, deadlock results.

### APP (inventory-service): `Waiting for order-service to be healthy before starting`
**Meaning:** inventory-service is also waiting for order-service. With both
waiting for the other, neither will ever start.

### HEALTH: `Health check failed: order-service instance-0000 — not healthy`
### HEALTH: `Health check failed: inventory-service instance-0000 — not healthy`
**Meaning:** Both services fail health checks because neither completes startup.
These look like independent failures but are causally linked.

### SCHEDULER: `Rescheduling order-service/instance-0000 (health check failure)`
### SCHEDULER: `Rescheduling inventory-service/instance-0000 (health check failure)`
**Meaning:** Both services are being rescheduled in a loop. This will repeat
indefinitely until the circular dependency is broken.

## Diagnostic Pattern
The circular deadlock is confirmed when:
1. Both services show repeated health check failures.
2. Both service APP logs show "waiting for [the other service]".
3. Neither service ever passes health checks or progresses past startup.
""",
)

doc(
    doc_id="config_INC-019",
    doc_type="config",
    incident_ids=["INC-019"],
    components=["APP", "HEALTH"],
    failure_pattern="circular_startup_dependency",
    tier=3,
    title="Configuration Reference: Startup Dependencies and Health Checks",
    content="""
# Configuration Reference: Startup Dependencies and Health Checks

## Health Check Types

| Type       | What it checks | Failure effect |
|------------|---------------|----------------|
| port       | TCP connection to $PORT | Container restart |
| http       | HTTP GET to /health path | Container restart |
| process    | Process is still running | Container restart |

## Health Check Configuration (manifest)

  ---
  applications:
  - name: order-service
    health-check-type: http
    health-check-http-endpoint: /health
    health-check-invocation-timeout: 10

## Startup Dependencies: Anti-Pattern
Do NOT implement startup dependencies that poll another service's health:
  # Anti-pattern: blocks startup until dependency is healthy
  while not is_healthy("inventory-service"):
      time.sleep(1)
  start_app()

## Startup Dependencies: Recommended Pattern
Implement lazy initialization — the app starts and passes health checks
immediately, then establishes connections to dependencies asynchronously:
  # Recommended: start first, connect later
  start_http_server()   # immediately healthy
  @background_task
  def connect_to_dependencies():
      wait_for_inventory_service()
      initialize_connection_pool()

## Key Principle
Health checks must pass independently of the availability of other services.
Services should tolerate dependency unavailability at startup and retry
connections in the background. This prevents circular deadlocks entirely.
""",
)

# ── INC-020: Autoscaler oscillation ──────────────────────────────────────────

doc(
    doc_id="runbook_INC-020",
    doc_type="runbook",
    incident_ids=["INC-020"],
    components=["AUTOSCALER", "CELL", "METRICS", "SCHEDULER"],
    failure_pattern="autoscaler_oscillation",
    tier=2,
    title="Runbook: Autoscaler Oscillation (Cooldown Too Short)",
    content="""
# Runbook: Autoscaler Oscillation (Cooldown Too Short)

## Symptom
Repeated scale-up and scale-down events in a regular rhythm. CPU never stabilises.
AUTOSCALER logs show more than 2 scale-up/scale-down cycles. SCHEDULER logs show
instance count cycling. The oscillation period is approximately equal to
instance_startup_time + cooldown_period.

## Diagnosis Steps

1. **Count scale-up/scale-down cycles.**
   Look for: `Scale-up triggered` and `Scale-down triggered` events in AUTOSCALER logs.
   Two or more complete cycles = oscillation (not a one-time adjustment).

2. **Measure the oscillation period.**
   Time between consecutive scale-up events = oscillation period.
   Expected: startup_time (55-65s) + cooldown (30s) + stabilisation (~10s) ≈ 95-105s.
   If the measured period matches this, cooldown misconfiguration is confirmed.

3. **Compare cooldown vs startup time.**
   Look for: `New instance ready: startup_time=62s` in CELL/HEALTH logs.
   If startup_time > cooldown_period, oscillation will occur.
   INC-020: startup=55-65s, cooldown=30s → cooldown expires before instance absorbs load.

4. **Check if CPU stabilises after scale-up.**
   If CPU drops temporarily after scale-up then spikes again when scale-down fires,
   the new instance IS absorbing load — but the cooldown is too short to see it.

5. **Distinguish from genuine load increase.**
   Oscillation: regular rhythm, CPU spikes align with scale-down events.
   Genuine load: irregular CPU growth, no correlation with scale events.

## Resolution
Increase cooldown period to exceed the maximum instance startup time:
  cf update-autoscaling-limits <app> --cooldown 120
  # Rule: cooldown > max_instance_startup_time + stabilisation_time

## Measuring Startup Time
Check CELL and HEALTH logs for the time between `container created` and
`health check passed` for recent instances. Use the 95th percentile.
""",
)

doc(
    doc_id="error_ref_INC-020",
    doc_type="error_ref",
    incident_ids=["INC-020"],
    components=["AUTOSCALER", "CELL", "SCHEDULER"],
    failure_pattern="autoscaler_oscillation",
    tier=2,
    title="Error Reference: Autoscaler Oscillation",
    content="""
# Error Reference: Autoscaler Oscillation

## Log Messages and Their Meanings

### AUTOSCALER: `Scale-up triggered: payments-api 2→4 instances (CPU=87%, threshold=80%)`
**Meaning:** CPU exceeded threshold. Two new instances starting.

### CELL: `Container created for instance-0002` (at T+0s)
### HEALTH: `Health check passed: instance-0002` (at T+62s)
**Meaning:** New instance took 62 seconds from creation to healthy. Cooldown (30s)
expired at T+30s — 32 seconds before this instance was ready to absorb load.

### AUTOSCALER: `Scale-down triggered: payments-api 4→2 instances (CPU=68%, threshold=80%)`
**Meaning:** CPU dropped below threshold (the new instances ARE absorbing load)
but the cooldown already expired, so scale-down fires immediately. The 2 instances
being removed were the ones that just became healthy — about to cause another CPU spike.

### AUTOSCALER: `Scale-up triggered: payments-api 2→4 instances (CPU=89%, threshold=80%)` (at T+95s)
**Meaning:** Second scale-up in the oscillation cycle. Same situation repeats.

## Oscillation Pattern Recognition
If you see this sequence repeated 3+ times:
  Scale-up → (cooldown elapses) → Scale-down → CPU spike → Scale-up → ...
...the diagnosis is autoscaler oscillation due to cooldown < startup time.
""",
)

doc(
    doc_id="config_INC-020",
    doc_type="config",
    incident_ids=["INC-020"],
    components=["AUTOSCALER"],
    failure_pattern="autoscaler_oscillation",
    tier=2,
    title="Configuration Reference: Autoscaler Cooldown Period",
    content="""
# Configuration Reference: Autoscaler Cooldown Period

## Critical Configuration Relationship

    cooldown_period MUST BE > instance_startup_time

If this condition is not met, autoscaler oscillation will occur.

## Autoscaler Parameters

| Parameter          | INC-020 Value | Recommended |
|--------------------|---------------|-------------|
| scale_up_threshold | 80% CPU       | 80% CPU     |
| scale_down_threshold | 50% CPU     | 50% CPU     |
| cooldown_period    | 30s           | >90s (measure startup first) |
| instance_startup_time | 55-65s   | measure per app |

## Measuring Instance Startup Time

  # From logs: time from container create to health check pass
  SELECT
    (julianday(health_pass) - julianday(container_create)) * 86400 AS startup_secs
  FROM instance_events
  ORDER BY startup_secs DESC
  LIMIT 10;  -- use 95th percentile, not mean

## Setting Cooldown

  cf update-autoscaling-limits <app> <min> <max> \
    --cooldown <seconds>

  # Rule: cooldown = max_startup_time + 30s stabilisation buffer
  # INC-020 fix: cf update-autoscaling-limits payments-api 2 8 --cooldown 120

## Operating Baseline
Autoscaler cooldown must exceed instance startup time.
INC-020: 30s cooldown vs 55-65s startup = oscillation.
Normal instance startup: 15-60s (varies by app size and cell load).
""",
)

# ── INC-021: BBS quorum loss ──────────────────────────────────────────────────

doc(
    doc_id="runbook_INC-021",
    doc_type="runbook",
    incident_ids=["INC-021"],
    components=["SCHEDULER", "CONTROLLER", "CELL", "ROUTER"],
    failure_pattern="bbs_quorum_loss",
    tier=3,
    title="Runbook: BBS Locket Quorum Loss (Diego Scheduling Unavailable)",
    content="""
# Runbook: BBS Locket Quorum Loss (Diego Scheduling Unavailable)

## Symptom
All new deployments fail immediately. `cf push` returns errors. Running applications
continue to serve traffic normally. SCHEDULER logs show Locket quorum failure.
Control plane is down; data plane is healthy.

## Diagnosis Steps

1. **Check SCHEDULER for Locket quorum errors.**
   Look for: `Locket: lost quorum — 1/3 nodes reachable`
   or `BBS: stepping down as master — quorum lost`
   This is the definitive indicator of BBS quorum loss.

2. **Verify running apps are healthy.**
   Check ROUTER for traffic — should be normal. Check HEALTH for running instances.
   If running apps are healthy and only new deployments fail, this is a control
   plane failure (BBS quorum), not a data plane failure.

3. **Identify the isolated node.**
   Look for: `Locket: node locket-2 unreachable (network partition)`
   The node identity tells you where the network partition is.

4. **Confirm the partition duration.**
   Note the first quorum loss timestamp and the recovery timestamp.
   INC-021: 4 minutes of quorum loss.

5. **Check CONTROLLER for failed push attempts.**
   Look for: `cf push rejected: SCHEDULER unavailable`
   These failures are expected during quorum loss — not a separate problem.

## Resolution
- Short-term: restore network connectivity to the isolated Locket node.
- If the node is unrecoverable: rebuild the Locket node via BOSH.
- After quorum is restored, BBS elects a new master and scheduling resumes automatically.

## Key Behaviour
BBS uses Raft consensus (via Locket). Quorum requires 2/3 nodes to be reachable.
Quorum loss stops all scheduling operations but does NOT affect running instances.
Running instances continue executing and receiving traffic through ROUTER independently.
""",
)

doc(
    doc_id="error_ref_INC-021",
    doc_type="error_ref",
    incident_ids=["INC-021"],
    components=["SCHEDULER", "CONTROLLER"],
    failure_pattern="bbs_quorum_loss",
    tier=3,
    title="Error Reference: BBS Locket Quorum Loss",
    content="""
# Error Reference: BBS Locket Quorum Loss

## Log Messages and Their Meanings

### SCHEDULER: `Locket: lost quorum — 1/3 nodes reachable (need 2/3)`
**Meaning:** Only 1 of the 3 Locket nodes is reachable. BBS cannot achieve
consensus. It will step down as master and refuse scheduling requests.

### SCHEDULER: `BBS: stepping down as master — quorum lost`
**Meaning:** BBS has stepped down. It will no longer accept placement requests
until quorum is restored. Currently-running instances are unaffected.

### CONTROLLER: `cf push rejected: SCHEDULER unavailable — BBS not master`
**Meaning:** A cf push was attempted during the quorum loss window. The CONTROLLER
received the request but SCHEDULER refused it because BBS is not master.

### SCHEDULER: `Locket: node locket-2 unreachable (network partition) — retrying`
**Meaning:** Locket is detecting and logging the network partition. It will
continue retrying connectivity to the isolated node.

### SCHEDULER: `Locket: quorum restored — 3/3 nodes reachable`
**Meaning:** All three Locket nodes are reachable again. BBS will re-elect a
master and resume accepting scheduling requests.

### SCHEDULER: `BBS: elected as master — resuming normal operations`
**Meaning:** BBS master election completed. cf push operations will now succeed.
""",
)

doc(
    doc_id="config_INC-021",
    doc_type="config",
    incident_ids=["INC-021"],
    components=["SCHEDULER"],
    failure_pattern="bbs_quorum_loss",
    tier=3,
    title="Configuration Reference: BBS Locket Quorum",
    content="""
# Configuration Reference: BBS Locket Quorum (Diego Consensus)

## Locket Quorum Requirements

| Locket Nodes | Quorum | Can Tolerate |
|--------------|--------|--------------|
| 3 (default)  | 2/3    | 1 node failure/partition |
| 5            | 3/5    | 2 node failures/partitions |

## Failure Scope
- BBS quorum loss affects: cf push, cf scale, new placements, cf delete.
- BBS quorum loss does NOT affect: running instances, traffic routing, health checks.
- Running apps continue serving traffic through ROUTER independently of BBS.

## Recovery
- Quorum restores automatically when the isolated/failed node reconnects.
- If the node is permanently failed: rebuild via BOSH `bosh recreate`.
- After quorum restore, BBS re-elects master within seconds.

## Monitoring
Alert on:
  pattern: "Locket: lost quorum"
  action: page on-call immediately (platform scheduling is down)

Do NOT rely on app health checks alone to detect BBS quorum loss — apps
continue to look healthy while scheduling is unavailable.

## Key Insight
INC-021: 1 of 3 BBS nodes isolated by network partition for 4 minutes.
During those 4 minutes, zero new deployments were possible. All running
routes continued serving traffic normally.
""",
)

# ── INC-022: Thread pool exhaustion from external latency ────────────────────

doc(
    doc_id="runbook_INC-022",
    doc_type="runbook",
    incident_ids=["INC-022"],
    components=["APP", "HEALTH", "ROUTER"],
    failure_pattern="thread_pool_exhaustion",
    tier=3,
    title="Runbook: Thread Pool Exhaustion from External Dependency Latency",
    content="""
# Runbook: Thread Pool Exhaustion from External Dependency Latency

## Symptom
App appears completely unresponsive — 503s from ROUTER, health check failures.
However, some requests still succeed (those that use cached responses or bypass
the slow external call). The app process is running. Restarting the app temporarily
resolves the issue but it recurs.

## Diagnosis Steps

1. **Check APP logs for external call latency.**
   Look for: `OAuth validation: 3800ms (normal: 80ms)`
   or `External service latency elevated: avg=8200ms`
   An external service that is normally fast has become slow.

2. **Check APP for thread pool status.**
   Look for: `thread pool: 18/20 waiting on OAuth` or
   `RejectedExecutionException: thread pool exhausted`
   Threads held waiting for the slow external call drain the thread pool.

3. **Check for partial success — the key diagnostic signal.**
   Look for: some requests returning 200 OK (cached or unauthenticated endpoints)
   while most return 503. If ANY requests succeed during the outage, the process
   is not crashed — thread pool exhaustion is the mechanism.

4. **Check HEALTH for timeout.**
   Look for: `health check timeout` after the thread pool exhaustion.
   HEALTH uses the same thread pool — when the pool is exhausted, health check
   requests queue and eventually timeout.

5. **Identify the external dependency.**
   The APP logs for external call latency identify the root cause.
   INC-022: OAuth provider latency spike to 8s (normal: 80ms).

## Resolution
- Short-term: add a timeout on external calls (e.g. 1-2s for OAuth, not 30s).
- Medium-term: implement circuit breaker pattern on external calls.
- Long-term: cache OAuth tokens to reduce external call frequency.

## Red Herrings
- Cached requests return 200 OK during the outage — do not conclude app is healthy.
- Health check failure appears after thread exhaustion — it is a consequence.
""",
)

doc(
    doc_id="error_ref_INC-022",
    doc_type="error_ref",
    incident_ids=["INC-022"],
    components=["APP", "HEALTH", "ROUTER"],
    failure_pattern="thread_pool_exhaustion",
    tier=3,
    title="Error Reference: Thread Pool Exhaustion",
    content="""
# Error Reference: Thread Pool Exhaustion

## Log Messages and Their Meanings

### APP: `OAuth validation latency: 80ms` → `OAuth validation latency: 3800ms`
**Meaning:** The external OAuth provider response time increased from 80ms (normal)
to 3800ms. Each authentication request now holds a thread for 3.8 seconds.

### APP: `thread pool: 18/20 waiting on OAuth`
**Meaning:** 18 of 20 available threads are held waiting for OAuth responses.
Only 2 threads are available for other requests — effectively exhausted.

### APP: `RejectedExecutionException: thread pool exhausted — rejecting request`
**Meaning:** All 20 threads are occupied. New requests are immediately rejected
without queuing. This is the moment the app becomes completely unresponsive.

### APP: `GET /products/123 → 200 OK (from cache, no auth required)` (during outage)
**Meaning:** A request that uses cached data and skips authentication succeeds.
This is the key indicator that the process is alive but the thread pool is exhausted.

### HEALTH: `health check timeout: connection refused on port 8080`
**Meaning:** The health check request was rejected because the thread pool is full.
This is a CONSEQUENCE of thread pool exhaustion, not the root cause.

### ROUTER: `503 Service Unavailable: payments-api (all instances unhealthy)`
**Meaning:** HEALTH marked all instances unhealthy due to timeouts. ROUTER has
no healthy backends. This is the final downstream consequence.

## OAuth Latency Thresholds

| Latency   | Impact |
|-----------|--------|
| ~80ms     | Normal — threads return quickly |
| >500ms    | Alert threshold — threads begin accumulating |
| >3800ms   | INC-022 value — thread pool exhaustion in ~45s |
""",
)

doc(
    doc_id="config_INC-022",
    doc_type="config",
    incident_ids=["INC-022"],
    components=["APP"],
    failure_pattern="thread_pool_exhaustion",
    tier=3,
    title="Configuration Reference: External Call Timeouts and Thread Pool",
    content="""
# Configuration Reference: External Call Timeouts and Thread Pool

## OAuth Latency Operating Baseline

| Metric                 | Normal | Alert  | INC-022 Value |
|------------------------|--------|--------|---------------|
| OAuth validation latency | ~80ms | >500ms | 3800ms        |
| Thread pool usage       | <50%  | >80%   | 100% (exhausted) |

## External Call Timeout Configuration

  # Java (OkHttpClient):
  OkHttpClient client = new OkHttpClient.Builder()
      .connectTimeout(1, TimeUnit.SECONDS)
      .readTimeout(2, TimeUnit.SECONDS)    # fail fast, don't hold threads
      .build();

  # Python (requests):
  requests.get(url, timeout=(1.0, 2.0))   # (connect_timeout, read_timeout)

  # Node.js (axios):
  axios.get(url, { timeout: 2000 })       # 2s total timeout

## Thread Pool Sizing

  # Rule: thread_pool_size > max_concurrent_requests * external_call_p99_latency / 1000
  # Example: 100 req/s * 2s timeout / 1 = 200 threads minimum

## Circuit Breaker Pattern
Implement circuit breaker on external OAuth calls:
- CLOSED: calls succeed normally
- OPEN: calls fail immediately after threshold failures (fast fail, no thread holding)
- HALF-OPEN: periodic test calls to detect recovery

Libraries: Hystrix (Java), resilience4j (Java), pybreaker (Python), cockatiel (Node.js)
""",
)

# ── INC-023: Partial blob upload ──────────────────────────────────────────────

doc(
    doc_id="runbook_INC-023",
    doc_type="runbook",
    incident_ids=["INC-023"],
    components=["BLOB_STORE", "BUILD_SERVICE", "CONTROLLER"],
    failure_pattern="partial_blob_upload",
    tier=2,
    title="Runbook: Partial Blob Upload (Checksum Mismatch on Deploy)",
    content="""
# Runbook: Partial Blob Upload (Checksum Mismatch on Deploy)

## Symptom
Deploy fails consistently with checksum mismatch error. The build succeeded
previously (or succeeds in the current attempt) but BLOB_STORE rejects the
droplet comparison. Retrying the deploy without code changes still fails.

## Diagnosis Steps

1. **Check BLOB_STORE for checksum mismatch.**
   Look for: `checksum mismatch: existing blob at key droplets/app-XXXX differs`
   or `partial upload detected: blob size=2.1GB expected=4.3GB`
   This confirms a corrupt blob is stored at the expected key.

2. **Check BUILD_SERVICE for the upload interruption.**
   Look for (in previous deploy logs): `upload interrupted` or `connection reset during upload`
   The partial blob was created during a previous interrupted deploy.

3. **Confirm the current build succeeds.**
   If BUILD_SERVICE shows `stage complete: droplet ready` but BLOB_STORE shows
   a checksum mismatch, the current build is fine — the old partial blob is the problem.

4. **Identify the blob key.**
   The BLOB_STORE error message includes the blob key (usually contains app-GUID).
   This is the partial blob that needs to be deleted.

5. **Verify the new deploy fails consistently.**
   If the deploy fails on every attempt with the same checksum error, the stale
   blob is blocking. If it fails intermittently, investigate BLOB_STORE availability.

## Resolution
Delete the partial blob from BLOB_STORE:
  # Via CF CLI (if operator access available):
  cf curl /v2/apps/<app-guid>/droplets
  # Identify and delete the corrupt droplet blob via the CF API or BOSH/S3 direct access.

Then redeploy:
  cf push <app>

## Red Herrings
- The build succeeds in the current run. The failure is in BLOB_STORE, not BUILD_SERVICE.
- Retrying the deploy will not help — the partial blob persists until manually deleted.
""",
)

doc(
    doc_id="error_ref_INC-023",
    doc_type="error_ref",
    incident_ids=["INC-023"],
    components=["BLOB_STORE", "BUILD_SERVICE"],
    failure_pattern="partial_blob_upload",
    tier=2,
    title="Error Reference: Partial Blob Upload (Checksum Mismatch)",
    content="""
# Error Reference: Partial Blob Upload (Checksum Mismatch)

## Log Messages and Their Meanings

### BLOB_STORE: `checksum mismatch: existing blob at key droplets/app-XXXX`
**Meaning:** BLOB_STORE found an existing file at the expected droplet key.
The checksum of the existing file does not match the expected checksum.
This indicates a partial or corrupted blob from a previous interrupted upload.

### BLOB_STORE: `partial upload detected: blob size=2.1GB, expected=4.3GB`
**Meaning:** The existing blob at the key is half the expected size. An upload
was interrupted and the partial file was not cleaned up.

### BUILD_SERVICE: `stage complete: droplet uploaded to blob-store` (current deploy)
**Meaning:** The current build succeeded and the new droplet was uploaded.
However, if BLOB_STORE then reports a checksum mismatch, the upload overwrote
the partial blob — but the checksum validation still fails.

### CONTROLLER: `Deploy failed: BLOB_STORE checksum validation error for app-XXXX`
**Meaning:** CONTROLLER received a BLOB_STORE error during deploy. The app
will not be updated. The current running version (if any) continues serving traffic.

### BUILD_SERVICE: `upload interrupted: connection reset at 2.1GB/4.3GB`
**Meaning:** (from a PREVIOUS deploy log) A network interruption occurred during
droplet upload. The partial file was written to BLOB_STORE and not cleaned up.
""",
)

doc(
    doc_id="config_INC-023",
    doc_type="config",
    incident_ids=["INC-023"],
    components=["BLOB_STORE"],
    failure_pattern="partial_blob_upload",
    tier=2,
    title="Configuration Reference: BLOB_STORE and Droplet Lifecycle",
    content="""
# Configuration Reference: BLOB_STORE and Droplet Lifecycle

## BLOB_STORE Behaviour
- BLOB_STORE does NOT implement transactional writes. If a network interruption
  occurs mid-upload, the partial file is written to the store.
- Partial blobs are NOT automatically cleaned up.
- Subsequent deploys find the partial blob at the same key, compare checksums,
  and fail with a mismatch error.

## Blob Keys
Droplets are stored with keys based on app GUID and checksum:
  droplets/<app-guid>/<droplet-checksum>

If an interrupted upload writes an incomplete file at this key, the next deploy
to the same key will find the partial blob.

## Cleanup Procedure

  # List droplets for an app:
  cf curl /v2/apps/<guid>/droplets

  # Delete a specific droplet blob (requires operator or CF API access):
  cf curl -X DELETE /v2/droplets/<droplet-guid>

  # For direct BOSH/S3 access:
  aws s3 rm s3://<bucket>/droplets/<app-guid>/<checksum>

## Prevention
- Use a BLOB_STORE implementation that supports atomic writes (S3 multipart
  with complete-or-abort semantics, not all implementations).
- Implement upload retry with exponential backoff in BUILD_SERVICE.
- Set a blob lifecycle policy to clean up incomplete multipart uploads after 24h.
""",
)

# ── INC-024: Readiness vs liveness health check ──────────────────────────────

doc(
    doc_id="runbook_INC-024",
    doc_type="runbook",
    incident_ids=["INC-024"],
    components=["APP", "CELL", "CONTROLLER", "HEALTH"],
    failure_pattern="readiness_check_blocking_deploy",
    tier=2,
    title="Runbook: Rolling Deploy Stalled by Readiness Check Failure",
    content="""
# Runbook: Rolling Deploy Stalled by Readiness Check Failure

## Symptom
Rolling deploy starts, first instance updates to v2, but the deploy stops
progressing. CONTROLLER shows deploy stalled. HEALTH logs show the canary
instance passing liveness (/health returns 200) but failing readiness
(/readiness returns 503). The deploy is blocked indefinitely.

## Diagnosis Steps

1. **Check HEALTH for liveness vs readiness distinction.**
   Look for: `liveness check passed: /health → 200`
   AND: `readiness check failed: /readiness → 503`
   If liveness passes but readiness fails, the instance is alive but not ready
   for traffic. The rolling deploy is waiting for readiness to proceed.

2. **Check APP logs for the readiness failure reason.**
   Look for: `readiness check: waiting for database connection pool to initialise`
   or `readiness check: dependency X not available`
   The readiness endpoint is designed to return 503 until dependencies are ready.

3. **Check CONTROLLER for the blocked deploy.**
   Look for: `Rolling deploy stalled: waiting for instance-0000 to pass readiness check`
   This confirms the deploy is blocked by readiness, not crashed.

4. **Determine if the readiness failure is transient or persistent.**
   Transient: /readiness returns 200 after a delay (e.g. 10-30s for DB connection).
   Persistent: /readiness continues returning 503 indefinitely — the dependency is unavailable.

5. **Check the dependency the readiness check is waiting for.**
   If the readiness endpoint checks a database, external API, or other service,
   verify that service is available. An unavailable dependency causes permanent
   readiness failure.

## Resolution
- If the dependency is unavailable: restore the dependency. The deploy will resume.
- If readiness timeout is too short: increase `health-check-invocation-timeout`.
- If the readiness check is too strict: consider allowing the deploy to proceed
  with instances that pass liveness but not yet readiness (traffic is withheld
  from unready instances automatically).

## Red Herrings
- HEALTH shows 200 from /health — the app is running. The deploy is not failing
  because the app crashed.
- The deploy appears stuck — but it is intentionally waiting for readiness.
""",
)

doc(
    doc_id="error_ref_INC-024",
    doc_type="error_ref",
    incident_ids=["INC-024"],
    components=["APP", "HEALTH", "CONTROLLER"],
    failure_pattern="readiness_check_blocking_deploy",
    tier=2,
    title="Error Reference: Readiness vs Liveness Check Distinction",
    content="""
# Error Reference: Readiness vs Liveness Check Distinction

## Log Messages and Their Meanings

### HEALTH: `liveness check passed: /health → 200 OK (instance-0000)`
**Meaning:** The app process is alive and responding to the /health endpoint.
The container will NOT be restarted. However, this does not mean traffic will
be routed to this instance.

### HEALTH: `readiness check failed: /readiness → 503 Service Unavailable (instance-0000)`
**Meaning:** The app is alive (liveness passed) but reports itself as not ready
to receive traffic. ROUTER will withhold this instance from the routing pool.
In a rolling deploy, CONTROLLER will not proceed to the next instance.

### APP: `readiness: waiting for database connection pool to initialise (pool: 0/20)`
**Meaning:** The /readiness endpoint returns 503 because the DB connection pool
has not yet established connections. The app is deliberately reporting unready.

### CONTROLLER: `Rolling deploy stalled: waiting for canary instance-0000 to pass readiness`
**Meaning:** The rolling deploy controller is blocked waiting for instance-0000's
readiness check to pass. The deploy will not update the next instance until this
passes. This can wait indefinitely if the dependency never becomes available.

### HEALTH: `readiness check passed: /readiness → 200 OK (instance-0000)`
**Meaning:** The dependency is now available. The instance is ready. The rolling
deploy will resume with the next instance.
""",
)

doc(
    doc_id="config_INC-024",
    doc_type="config",
    incident_ids=["INC-024"],
    components=["HEALTH", "CONTROLLER"],
    failure_pattern="readiness_check_blocking_deploy",
    tier=2,
    title="Configuration Reference: Liveness vs Readiness Health Checks",
    content="""
# Configuration Reference: Liveness vs Readiness Health Checks

## Two Health Check Types

| Type       | Path        | Failure Effect | Rolling Deploy Effect |
|------------|-------------|----------------|-----------------------|
| Liveness   | /health     | Container restart | No effect on deploy progression |
| Readiness  | /readiness  | ROUTER withholds traffic | Deploy blocked until passes |

## Manifest Configuration

  ---
  applications:
  - name: payments-api
    health-check-type: http
    health-check-http-endpoint: /health          # liveness endpoint
    readiness-health-check-type: http
    readiness-health-check-http-endpoint: /readiness   # readiness endpoint
    health-check-invocation-timeout: 10          # seconds per probe

## Endpoint Implementation Best Practice

  # /health (liveness) — return 200 if process is running
  @app.route('/health')
  def health():
      return {'status': 'alive'}, 200   # always 200 unless process is broken

  # /readiness — return 200 only if ready to serve traffic
  @app.route('/readiness')
  def readiness():
      if db_pool.available_connections > 0:
          return {'status': 'ready'}, 200
      return {'status': 'waiting for DB'}, 503

## Rolling Deploy and Readiness
The rolling deploy controller waits for readiness to pass before proceeding
to the next instance. If readiness never passes, the deploy hangs indefinitely.

To allow the deploy to proceed even with a slow readiness probe:
  health-check-invocation-timeout: 60   # give the instance more time to become ready

To force the deploy to proceed regardless of readiness (use with caution):
  cf push <app> --no-wait   # does not wait for health checks
""",
)

# ── INC-025: Platform mTLS cert expiry ───────────────────────────────────────

doc(
    doc_id="runbook_INC-025",
    doc_type="runbook",
    incident_ids=["INC-025"],
    components=["APP", "CONTROLLER", "HEALTH", "METRICS", "ROUTER"],
    failure_pattern="platform_cert_expiry",
    tier=3,
    title="Runbook: Platform mTLS Certificate Expiry (All Inter-Service Communication Fails)",
    content="""
# Runbook: Platform mTLS Certificate Expiry (All Inter-Service Communication Fails)

## Symptom
All internal service-to-service communication fails simultaneously. TLS handshake
errors appear in APP logs. ROUTER reports mTLS verification failed for all backends.
CERT WARNING messages appear in logs approximately 6 hours before the outage.
The failure is abrupt — 100% success until the second of expiry, then 100% failure.

## Diagnosis Steps

1. **Search for CERT WARNING entries before the failure.**
   Look for: `CERT WARNING: internal mTLS cert expires in 6h12m: /certs/internal-ca.crt`
   in CONTROLLER and ROUTER logs, hours before the failure window.

2. **Identify the failure cliff.**
   Look for: abrupt onset of TLS errors at a specific timestamp.
   All apps should show TLS errors at approximately the same second — this
   simultaneity distinguishes platform cert expiry from individual service issues.

3. **Confirm platform-wide scope.**
   Check multiple apps and components. If all show TLS errors at the same time,
   a platform cert (not a service-specific cert) expired.
   Distinguish from INC-005 (single service binding cert — only one service affected).

4. **Check ROUTER for platform-level impact.**
   Look for: `mTLS verification failed: all backend connections rejected`
   AND: `503: all internal routes down — cert expired`
   If ALL routes are down simultaneously, the platform CA cert expired.

5. **Identify the cert that expired.**
   CERT WARNING includes the cert path: `/certs/internal-ca.crt`
   This is the internal CA certificate — used by all platform components
   for mutual TLS authentication.

6. **Note the 5-hour gap.**
   In INC-025, CERT WARNING appears at T-6h12m and the failure at T.
   5+ hours of routine DEBUG logs appear between them. The warning and failure
   are causally connected despite the gap.

## Resolution
Rotate the expired certificate via BOSH:
  bosh -d cf rotate-certs
  # This triggers a rolling cert rotation across all platform components.

## Prevention
Configure alerting on CERT WARNING log patterns with >48h lead time.
""",
)

# (error_ref and config for INC-025 are shared with INC-005 above)


# ============================================================================
# WRITE OUTPUT
# ============================================================================

output_path = "./data/doc_corpus.jsonl"
with open(output_path, "w", encoding="utf-8") as f:
    for d in docs:
        f.write(json.dumps(d) + "\n")

print(f"Written {len(docs)} documents to {output_path}")
print()

# Summary by type
from collections import Counter
by_type = Counter(d["doc_type"] for d in docs)
for t, count in sorted(by_type.items()):
    print(f"  {t:15}: {count}")

print()
print("Incident coverage:")
all_inc_ids = set()
for d in docs:
    all_inc_ids.update(d["incident_ids"])
print(f"  {len(all_inc_ids)} incidents covered: {sorted(all_inc_ids)}")

print()
print("Multi-incident docs (shared knowledge):")
for d in docs:
    if len(d["incident_ids"]) > 1:
        print(f"  {d['doc_id']}: {d['incident_ids']}")
