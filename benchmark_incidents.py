#!/usr/bin/env python3
"""
benchmark_incidents.py
----------------------
Static data file: the 25 synthetic PaaS incidents used by the benchmark.

This file is data, not a script. It exposes a single module-level constant,
BENCHMARK_CASES, which is a list of dicts — one per incident.

It is imported by:
  - run_benchmark.py    : iterates over BENCHMARK_CASES to build the run input
  - Results/evaluate.py : maps incident_id -> case to score deterministic
                          retrieval, trace, and answer-keyword metrics

To add or modify an incident, edit BENCHMARK_CASES below. Each entry has:

  incident_id      : INC-001 … INC-025
  tier             : 1 (simple), 2 (cross-component), 3 (complex/distributed)
  app_id           : the affected application identifier
  org              : the org the app belongs to
  question         : operator-style question referencing the actual symptom
                     and application so the retrieval must target this incident
  where_filter     : optional ChromaDB metadata pre-filter (or None)
  retrieval_signals: short distinctive substrings from the real log messages —
                     at least ONE must appear in the retrieved chunks to pass
  answer_required  : ALL of these substrings (case-insensitive) must appear
                     in the LLM answer for full_credit
  answer_partial   : if answer_required fails, any hit here gives partial credit
"""

BENCHMARK_CASES = [

    # ── TIER 1 — Single-component failures ──────────────────────────────────

    {
        "incident_id": "INC-001",
        "tier": 1,
        "app_id": "app-q0k5oz",
        "org": "org-payments",
        "question": (
            "payments-api (app-q0k5oz) in org-payments is crash-looping: "
            "the platform health check reports 'connection refused' on port 8080 "
            "but the container starts successfully. What is the root cause?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "port 8080",
            "port 3000",
            "health check failed",
            "crash loop",
        ],
        "answer_required": ["3000", "8080"],
        "answer_partial":  ["port", "health check", "$PORT"],
    },

    {
        "incident_id": "INC-002",
        "tier": 1,
        "app_id": "app-ocmoe0",
        "org": "org-analytics",
        "question": (
            "app-ocmoe0 in org-analytics was killed by the platform with "
            "'disk_quota_exceeded'. The app was running fine for hours before "
            "this. What is consuming the disk and what should be changed to "
            "prevent recurrence?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "disk quota",
            "no space left on device",
            "app.log",
            "container_disk_usage",
        ],
        "answer_required": ["disk", "log"],
        "answer_partial":  ["quota", "rotation", "ephemeral"],
    },

    {
        "incident_id": "INC-003",
        "tier": 1,
        "app_id": "app-ia54l8",
        "org": "org-devops",
        "question": (
            "app-ia54l8 in org-devops crashes immediately on startup with "
            "exit_status=1. The stderr shows a network error when trying to "
            "connect to its bound PostgreSQL service. What is causing the "
            "failure?"
        ),
        "where_filter": {"level": "ERROR"},
        "retrieval_signals": [
            "gaierror",
            "Name or service not known",
            "postgres-svc",
        ],
        "answer_required": ["dns", "postgres"],
        "answer_partial":  ["hostname", "resolve", "service"],
    },

    {
        "incident_id": "INC-004",
        "tier": 1,
        "app_id": "app-z5lhyd",
        "org": "org-payments",
        "question": (
            "app-z5lhyd in org-payments keeps crashing with 'memory_limit_exceeded'. "
            "Metrics show memory climbing steadily before each crash. "
            "What JVM error is causing the container to be killed?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "OutOfMemoryError",
            "memory_bytes",
            "OOM kill",
            "99% quota",
        ],
        "answer_required": ["OutOfMemoryError", "heap"],
        "answer_partial":  ["memory", "gc", "quota", "512"],
    },

    {
        "incident_id": "INC-005",
        "tier": 1,
        "app_id": "app-p8hcke",
        "org": "org-platform",
        "question": (
            "app-p8hcke in org-platform suddenly started returning 503s at "
            "midnight. The app's background queue processor logs show "
            "'CERTIFICATE_VERIFY_FAILED' when connecting to its RabbitMQ binding. "
            "What happened and how should it be fixed?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "CERTIFICATE_VERIFY_FAILED",
            "certificate has expired",
            "amqps://",
        ],
        "answer_required": ["certificate", "expired"],
        "answer_partial":  ["tls", "ssl", "rabbitmq", "bind"],
    },

    {
        "incident_id": "INC-006",
        "tier": 1,
        "app_id": "app-7zwece",
        "org": "org-devops",
        "question": (
            "app-7zwece in org-devops is failing to stage. The build log shows "
            "'EINTEGRITY: sha512 integrity check failed for express@4.18.2'. "
            "The same app deployed successfully last week. What is the root cause?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "EINTEGRITY",
            "sha512",
            "express@4.18.2",
            "buildpack cache",
        ],
        "answer_required": ["integrity", "cache"],
        "answer_partial":  ["npm", "sha", "corrupt"],
    },

    {
        "incident_id": "INC-007",
        "tier": 1,
        "app_id": "app-bcwwwh",
        "org": "org-analytics",
        "question": (
            "app-bcwwwh in org-analytics deployed successfully — Diego shows "
            "2/2 instances running — but every request to api.example.com "
            "returns 502. The router logs 'No route registered'. What was "
            "missed during deployment?"
        ),
        "where_filter": {"level": "WARN"},
        "retrieval_signals": [
            "No route registered",
            "502",
            "api.example.com",
        ],
        "answer_required": ["route", "map"],
        "answer_partial":  ["502", "backend", "cf map-route"],
    },

    # ── TIER 2 — Cross-component failures ───────────────────────────────────

    {
        "incident_id": "INC-008",
        "tier": 2,
        "app_id": "app-sxbvtt",
        "org": "org-payments",
        "question": (
            "app-sxbvtt (payments-api) in org-payments is returning sustained "
            "503s. Scaling out to more instances did not help. The metrics show "
            "db_pool_active_connections=20/20. What is the actual bottleneck "
            "and why did adding instances not resolve it?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "db_pool_active_connections=20",
            "connection pool exhausted",
            "Slow query",
        ],
        "answer_required": ["slow quer", "pool"],
        "answer_partial":  ["database", "connection", "index"],
    },

    {
        "incident_id": "INC-009",
        "tier": 2,
        "app_id": "app-nwgzbp",
        "org": "org-payments",
        "question": (
            "All 8 instances of app-nwgzbp in org-payments crashed simultaneously. "
            "Diego rescheduled them but they are failing health checks again. "
            "The cell metrics show cell-009 memory_available=0MB. "
            "What happened and why are the rescheduled instances also unhealthy?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "cell-009",
            "cell OOM",
            "Rescheduling",
            "cell overloaded",
        ],
        "answer_required": ["cell", "memory", "reschedule"],
        "answer_partial":  ["OOM", "pressure", "capacity"],
    },

    {
        "incident_id": "INC-010",
        "tier": 2,
        "app_id": "app-17shto",
        "org": "org-payments",
        "question": (
            "order-service (app-17shto) in org-payments was redeployed to v2.4.1 "
            "and is now logging 'ECONNREFUSED' for every call to inventory-svc. "
            "Both apps are running. The network logs show 'Outbound policy DENY'. "
            "What is the root cause and how is it fixed?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "DENY",
            "silk.daemon",
            "network-policy",
            "ECONNREFUSED",
        ],
        "answer_required": ["network policy", "deny"],
        "answer_partial":  ["policy", "blocked", "cf add-network-policy"],
    },

    {
        "incident_id": "INC-011",
        "tier": 2,
        "app_id": "app-8ygxbn",
        "org": "org-analytics",
        "question": (
            "app-8ygxbn (Python) in org-analytics builds without error but "
            "crashes on startup with 'ImportError: cannot import name "
            "typing_extensions from numpy'. requirements.txt pins numpy==1.24. "
            "What is wrong?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "numpy",
            "1.21",
            "ImportError",
            "buildpack default",
        ],
        "answer_required": ["numpy", "version"],
        "answer_partial":  ["buildpack", "1.24", "1.21", "pin"],
    },

    {
        "incident_id": "INC-012",
        "tier": 2,
        "app_id": "app-s0886r",
        "org": "org-devops",
        "question": (
            "Multiple deployments for app-s0886r and other apps in org-devops "
            "are failing during staging with '429 toomanyrequests' from "
            "registry-1.docker.io. Deployments were fine this morning. "
            "What is happening and what is the fix?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "toomanyrequests",
            "rate limit",
            "registry-1.docker.io",
            "429",
        ],
        "answer_required": ["rate limit", "docker"],
        "answer_partial":  ["registry", "pull", "authenticated"],
    },

    {
        "incident_id": "INC-013",
        "tier": 2,
        "app_id": "app-dj02vx",
        "org": "org-platform",
        "question": (
            "Deploying app-dj02vx in org-platform is stuck: cf bind-service "
            "for redis-cache has been running for over 30 seconds and the "
            "deployment pipeline is blocked. The broker logs show Redis master "
            "unreachable. What is causing the bind to time out?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "Bind timeout",
            "broker_timeout",
            "Redis",
            "master node unreachable",
        ],
        "answer_required": ["broker", "redis"],
        "answer_partial":  ["timeout", "service broker", "failover"],
    },

    {
        "incident_id": "INC-014",
        "tier": 2,
        "app_id": "app-eeyjwy",
        "org": "org-payments",
        "question": (
            "app-eeyjwy in org-payments shows a spike of 422/500 errors during "
            "a rolling deploy from v1 to v2. No instances are crashing. "
            "The logs show 'missing field idempotency_key' and the autoscaler "
            "fired during the deploy window. What is the root cause?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "mixed pool",
            "idempotency_key",
            "v1",
            "v2",
        ],
        "answer_required": ["version", "mixed"],
        "answer_partial":  ["autoscaler", "schema", "incompatible"],
    },

    {
        "incident_id": "INC-015",
        "tier": 2,
        "app_id": "app-7c09jg",
        "org": "org-platform",
        "question": (
            "Logs and metrics for app-7c09jg and several other apps in "
            "org-platform appear to be missing or delayed. The doppler metrics "
            "show buffer fill rates near 98%. What platform component is "
            "failing and what is the impact?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "Doppler buffer",
            "dropping envelopes",
            "backpressure",
            "messages dropped",
        ],
        "answer_required": ["doppler", "drop"],
        "answer_partial":  ["loggregator", "buffer", "backpressure"],
    },

    {
        "incident_id": "INC-016",
        "tier": 2,
        "app_id": "app-kch0ri",
        "org": "org-platform",
        "question": (
            "Diego has stopped receiving cell heartbeats and the routing table "
            "has gone stale, causing cell evacuations. The NATS metrics show "
            "message rate=48000/s against a normal of 2000/s. "
            "What is the root cause of the message bus saturation?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "NATS",
            "48000",
            "slow consumer",
            "metrics-agent",
        ],
        "answer_required": ["nats", "message"],
        "answer_partial":  ["saturated", "runaway", "publisher"],
    },

    {
        "incident_id": "INC-017",
        "tier": 2,
        "app_id": "app-q16e4q",
        "org": "org-analytics",
        "question": (
            "app-q16e4q and several co-located apps on cell-012 in org-analytics "
            "are failing health checks even though they are running. The cell "
            "CPU metric shows 94% sustained usage. What is causing the health "
            "check timeouts?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "cell-012",
            "cpu_usage",
            "94%",
            "noisy neighbor",
        ],
        "answer_required": ["cpu", "cell-012"],
        "answer_partial":  ["throttle", "noisy neighbor", "health check"],
    },

    # ── TIER 3 — Complex multi-factor / distributed failures ─────────────────

    {
        "incident_id": "INC-018",
        "tier": 3,
        "app_id": "app-7o9r07",
        "org": "org-payments",
        "question": (
            "After a blue-green swap for app-7o9r07 in org-payments, users "
            "saw 502 errors for about 45 seconds then traffic recovered on its "
            "own. The router logs show requests hitting a 'deregistered blue "
            "instance'. What caused the transient 502s and why did they "
            "self-resolve?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "stale backend",
            "deregistered",
            "cache",
            "TTL",
        ],
        "answer_required": ["cache", "route"],
        "answer_partial":  ["stale", "ttl", "blue", "expired"],
    },

    {
        "incident_id": "INC-019",
        "tier": 3,
        "app_id": "app-sudja3",
        "org": "org-platform",
        "question": (
            "auth-service (app-sudja3) and gateway-service (app-stq12y) in "
            "org-platform were deployed together. Both crashed after exactly "
            "90 seconds with health_check_timeout. The logs show each service "
            "polling the other's /health endpoint indefinitely. "
            "What architectural pattern caused this deadlock?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "gateway not ready",
            "auth not ready",
            "circular",
            "poll",
        ],
        "answer_required": ["circular", "dependency"],
        "answer_partial":  ["deadlock", "waiting", "health"],
    },

    {
        "incident_id": "INC-020",
        "tier": 3,
        "app_id": "app-ec0i16",
        "org": "org-payments",
        "question": (
            "The autoscaler for app-ec0i16 in org-payments is oscillating: "
            "it scales up every ~30 seconds then scales back down before the "
            "new instances are healthy. CPU rebounds to >65% in each cycle. "
            "What configuration mismatch is causing autoscaler thrashing?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "cooldown",
            "Scale up",
            "Scale down",
            "rebounded",
        ],
        "answer_required": ["cooldown", "startup"],
        "answer_partial":  ["oscillat", "thrash", "60s"],
    },

    {
        "incident_id": "INC-021",
        "tier": 3,
        "app_id": "app-fexcru",
        "org": "org-platform",
        "question": (
            "cf push for app-fexcru and all other apps in org-platform is "
            "being rejected with 'scheduler unavailable'. Running apps are "
            "unaffected. The BBS logs show 'BBS is not the active master'. "
            "What distributed system failure is blocking new deployments?"
        ),
        "where_filter": {"level": "ERROR"},
        "retrieval_signals": [
            "quorum lost",
            "Locket",
            "BBS is not the active master",
            "scheduling suspended",
        ],
        "answer_required": ["quorum", "bbs"],
        "answer_partial":  ["locket", "scheduler", "partition"],
    },

    {
        "incident_id": "INC-022",
        "tier": 3,
        "app_id": "app-0nl9y6",
        "org": "org-payments",
        "question": (
            "app-0nl9y6 (payments-api) in org-payments became completely "
            "unresponsive with no code changes. Logs show OAuth validate "
            "latency climbed from 80ms to 3800ms, then "
            "'RejectedExecutionException: no threads available'. "
            "What external dependency caused the full outage?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "OAuth",
            "thread pool",
            "3800ms",
            "RejectedExecutionException",
        ],
        "answer_required": ["oauth", "thread pool"],
        "answer_partial":  ["latency", "exhausted", "cascade"],
    },

    {
        "incident_id": "INC-023",
        "tier": 3,
        "app_id": "app-8fxpjz",
        "org": "org-devops",
        "question": (
            "app-8fxpjz in org-devops built successfully but every deploy "
            "attempt fails with 'Checksum mismatch: expected sha256=a1b2c3 "
            "got d4e5f6'. The blob store logs show 'Blob key conflict: "
            "partial blob exists'. Previous deploys worked. What is wrong?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "Checksum mismatch",
            "partial blob",
            "Blob key conflict",
            "Upload interrupted",
        ],
        "answer_required": ["partial", "blob"],
        "answer_partial":  ["checksum", "upload", "interrupted"],
    },

    {
        "incident_id": "INC-024",
        "tier": 3,
        "app_id": "app-oghb8v",
        "org": "org-payments",
        "question": (
            "A rolling deploy for app-oghb8v v3.1 in org-payments has been "
            "paused for 5 minutes. The canary instance passes its liveness "
            "check (HTTP 200) but the readiness endpoint at /readiness returns "
            "503 with 'cache not warmed'. Why is the deploy blocked and what "
            "is the fix?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "readiness",
            "503",
            "cache not warmed",
            "liveness",
        ],
        "answer_required": ["readiness", "503"],
        "answer_partial":  ["canary", "cache", "warmed"],
    },

    {
        "incident_id": "INC-025",
        "tier": 3,
        "app_id": "app-hk5m1k",
        "org": "org-platform",
        "question": (
            "All inter-service communication in org-platform failed "
            "simultaneously with 'TLS handshake error: certificate has expired'. "
            "The logs show a CERT WARNING was emitted 6 hours earlier. "
            "Why was the outage not prevented and how should this be detected "
            "automatically in future?"
        ),
        "where_filter": None,
        "retrieval_signals": [
            "CERT WARNING",
            "expires in",
            "mTLS",
            "certificate has expired",
        ],
        "answer_required": ["certificate", "warn"],
        "answer_partial":  ["expire", "alert", "monitor", "automat"],
    },
]
