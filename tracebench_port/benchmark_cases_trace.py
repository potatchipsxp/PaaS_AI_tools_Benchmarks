#!/usr/bin/env python3
"""
benchmark_cases_trace.py
-------------------------
Phase 2C: the TraceBench analog of benchmark_incidents.py. Static data file
holding BENCHMARK_CASES_TRACE — the operator-style question for each of the
27 cases admitted in data/ground_truth_trace.json.

Same anti-leakage discipline as benchmark_incidents.py and spec section 2C:
each question describes the observed SYMPTOM (grounded in the case's real
evidence_anchor text — never invented), scoped to the case's trace_id the
way PaaS questions are scoped to an app_id. It never names the injected
fault, its category, or which specific node is affected — that's exactly
what the agent has to find.

`answer_required` is a first-pass deterministic scoring key using real
technical vocabulary from the evidence (never the internal fault_name
codes like "killDN" — the agent has no reason to know those). This is
provisional pending the Phase 5 rewrite the spec calls for; it does not yet
account for the affected_component resolution issue noted in CHANGELOG_PORT
(5 cases where the recorded affected_component is the observing client, not
the actual target datanode — the datanode's real identity is only present
as a bare IP in the evidence text for those cases).

`retrieval_signals` is left empty pending Phase 3 (doc corpus doesn't exist
yet) rather than speculated.

Ground truth (fault_name, tier, affected_component, evidence_anchor) stays
isolated in ground_truth_trace.json / eval_tracebench.sqlite — not
duplicated here beyond what PaaS's own benchmark_incidents.py exposes
alongside its questions (tier, and scoring keyword lists).
"""

BENCHMARK_CASES_TRACE = [

    # ── TIER 1 — Single-component failures ──────────────────────────────

    {
        "case_id": "TB-011",
        "tier": 1,
        "trace_id": "03FB08C229C2844D",
        "question": (
            "During a read-only HDFS workload (instance 03FB08C229C2844D), some "
            "requests are failing with 'connection refused' when trying to reach a "
            "specific datanode, and the client subsequently avoids that node for "
            "the rest of the request. What is the root cause?"
        ),
        "answer_required": ["connection refused"],
        "answer_partial": ["unreachable", "datanode", "deadNodes"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-013",
        "tier": 1,
        "trace_id": "016683939AD657E8",
        "question": (
            "During a read-only HDFS workload (instance 016683939AD657E8), connection "
            "attempts to a specific datanode are being refused outright — the "
            "client gets an immediate 'connection refused' rather than a timeout, "
            "and moves on to a different replica. What is the root cause?"
        ),
        "answer_required": ["connection refused"],
        "answer_partial": ["datanode", "unreachable", "process"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-016",
        "tier": 1,
        "trace_id": "04737D2A819A255A",
        "question": (
            "During a read-only HDFS workload (instance 04737D2A819A255A), the client is "
            "unable to route to a specific datanode at the network level — "
            "connection attempts fail immediately with a network-unreachable style "
            "error rather than a timeout or refusal. What is the root cause?"
        ),
        "answer_required": ["no route to host"],
        "answer_partial": ["unreachable", "network", "datanode"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-017",
        "tier": 1,
        "trace_id": "47781BAF44C33DBE",
        "question": (
            "During a write HDFS workload (instance 47781BAF44C33DBE), the step where a "
            "datanode begins accepting a new block for write is measurably slower "
            "on one datanode than on its peers handling the same workload, though "
            "no errors are thrown. What is the root cause?"
        ),
        "answer_required": ["write", "datanode"],
        "answer_partial": ["slow", "block receiver", "latency"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-020",
        "tier": 1,
        "trace_id": "02C8C76629FB0876",
        "question": (
            "During a read HDFS workload (instance 02C8C76629FB0876), connection "
            "attempts to a specific datanode aren't refused outright — they "
            "simply hang until a 60-second timeout expires, as if the process is "
            "alive but not responding. What is the root cause?"
        ),
        "answer_required": ["timeout"],
        "answer_partial": ["hang", "unresponsive", "datanode"],
        "retrieval_signals": [],
    },

    # ── TIER 2 — Cross-component / performance degradation ─────────────

    {
        "case_id": "TB-012",
        "tier": 2,
        "trace_id": "0309988149E3639A",
        "question": (
            "During a read-only HDFS workload (instance 0309988149E3639A), some "
            "requests fail with a 'no route to host' network error when trying to "
            "reach a specific datanode, and the client marks that node as "
            "unreachable for the remainder of the operation. What is the root "
            "cause?"
        ),
        "answer_required": ["no route to host"],
        "answer_partial": ["network", "unreachable", "datanode"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-018",
        "tier": 2,
        "trace_id": "C4113E01C484F2EB",
        "question": (
            "During a read HDFS workload (instance C4113E01C484F2EB), one datanode is "
            "measurably slower than its peers across multiple block-transfer "
            "operations, though every request still eventually completes "
            "successfully. What is the root cause?"
        ),
        "answer_required": ["slow", "datanode"],
        "answer_partial": ["latency", "degraded", "network"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-019",
        "tier": 2,
        "trace_id": "5BBFC8999598FAD8",
        "question": (
            "During a write HDFS workload (instance 5BBFC8999598FAD8), block-receive "
            "operations across the cluster are running somewhat slower than the "
            "historical baseline overall, without any single datanode standing "
            "out as dramatically worse than the rest. What is the root cause?"
        ),
        "answer_required": ["slow", "cluster"],
        "answer_partial": ["latency", "network", "degraded"],
        "retrieval_signals": [],
    },

    # ── TIER 3 — Multi-factor / subtle / combination faults ────────────

    {
        "case_id": "TB-001",
        "tier": 3,
        "trace_id": "99B14FE25B6D4910",
        "question": (
            "During a mixed read/write/RPC HDFS workload (instance 99B14FE25B6D4910), "
            "clients are reporting intermittent slow block transfers — some "
            "requests take noticeably longer than others, though nothing fails "
            "outright. What is the root cause?"
        ),
        "answer_required": ["slow", "datanode"],
        "answer_partial": ["latency", "intermittent", "block transfer"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-002",
        "tier": 3,
        "trace_id": "3722734FFCFC8E63",
        "question": (
            "During a mixed read/write/RPC HDFS workload (instance 3722734FFCFC8E63), "
            "write operations are completing, but the client-side block "
            "verification step after some writes is taking dramatically longer "
            "than expected — clients appear to hang briefly right after finishing "
            "a block write. What is the root cause?"
        ),
        "answer_required": ["verif", "slow"],
        "answer_partial": ["latency", "block", "hang"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-003",
        "tier": 3,
        "trace_id": "0166FE80C78C3CF7",
        "question": (
            "During a mixed read/write/RPC HDFS workload (instance 0166FE80C78C3CF7), "
            "some read requests are failing immediately with a "
            "checksum-configuration error before any data is transferred, as if "
            "the block's checksum metadata itself is malformed. What is the root "
            "cause?"
        ),
        "answer_required": ["checksum"],
        "answer_partial": ["corrupt", "metadata", "malformed"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-004",
        "tier": 3,
        "trace_id": "DAB15CFA9D91F989",
        "question": (
            "During a mixed read/write/RPC HDFS workload (instance DAB15CFA9D91F989), "
            "requests are succeeding, but one specific datanode is responding to "
            "block-transfer requests much more slowly than the rest of the "
            "cluster. What is the root cause?"
        ),
        "answer_required": ["slow", "datanode"],
        "answer_partial": ["latency", "network", "degraded"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-005",
        "tier": 3,
        "trace_id": "A051712AA7AE25A9",
        "question": (
            "During a mixed read/write/RPC HDFS workload (instance A051712AA7AE25A9), "
            "the block-verification step following writes is intermittently slow "
            "— most writes verify quickly but a subset take far longer than the "
            "rest. What is the root cause?"
        ),
        "answer_required": ["verif", "slow"],
        "answer_partial": ["latency", "intermittent", "block"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-006",
        "tier": 3,
        "trace_id": "010AD72099CF744E",
        "question": (
            "During a mixed read/write/RPC HDFS workload (instance 010AD72099CF744E), "
            "some requests are failing outright with a 'connection refused' error "
            "when trying to reach specific datanodes, while other requests "
            "complete normally. What is the root cause?"
        ),
        "answer_required": ["connection refused"],
        "answer_partial": ["datanode", "unreachable"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-007",
        "tier": 3,
        "trace_id": "0207F6FDA149694E",
        "question": (
            "During a read-only HDFS workload (instance 0207F6FDA149694E), some file "
            "reads are failing with a checksum mismatch on the data itself, even "
            "though the read request otherwise proceeds normally up to that "
            "point. What is the root cause?"
        ),
        "answer_required": ["checksum"],
        "answer_partial": ["corrupt", "mismatch", "block"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-008",
        "tier": 3,
        "trace_id": "017819C0E34B6EFF",
        "question": (
            "During a read-only HDFS workload (instance 017819C0E34B6EFF), some read "
            "requests fail immediately with an invalid checksum-configuration "
            "error before any block data is even transferred — the checksum "
            "parameters themselves look malformed. What is the root cause?"
        ),
        "answer_required": ["checksum"],
        "answer_partial": ["corrupt", "metadata", "malformed"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-009",
        "tier": 3,
        "trace_id": "012D78362237F73E",
        "question": (
            "During a read-only HDFS workload (instance 012D78362237F73E), some reads "
            "are failing because the block on disk is much shorter than its "
            "recorded length — as if part of the block's data is simply missing. "
            "What is the root cause?"
        ),
        "answer_required": ["block", "length"],
        "answer_partial": ["missing", "truncated", "mismatch"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-010",
        "tier": 3,
        "trace_id": "002CE32105569C3C",
        "question": (
            "During a read-only HDFS workload (instance 002CE32105569C3C), read "
            "requests are failing immediately with implausible "
            "checksum-configuration values — a checksum type and byte-count that "
            "don't correspond to any valid configuration — as if the block's "
            "metadata file itself is truncated or partially missing. What is the "
            "root cause?"
        ),
        "answer_required": ["checksum", "metadata"],
        "answer_partial": ["corrupt", "truncated", "invalid"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-014",
        "tier": 3,
        "trace_id": "0308449B3F12B0A3",
        "question": (
            "During a read-only HDFS workload (instance 0308449B3F12B0A3), some reads "
            "fail because the datanode reports that the requested block simply "
            "isn't valid or present on it, even though the read request was "
            "routed there. What is the root cause?"
        ),
        "answer_required": ["block", "not valid"],
        "answer_partial": ["missing", "invalid", "replica"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-015",
        "tier": 3,
        "trace_id": "44C66D6F37A41CF3",
        "question": (
            "During an HDFS workload (instance 44C66D6F37A41CF3), one particular "
            "operation involved in verifying block data after transfer is taking "
            "drastically longer to complete on this trace than it does "
            "elsewhere, even though no errors are being raised. What is the root "
            "cause?"
        ),
        "answer_required": ["verif", "slow"],
        "answer_partial": ["latency", "metadata", "block"],
        "retrieval_signals": [],
    },

    # ── TIER 0 — Normal controls (negative controls, no fault) ─────────

    {
        "case_id": "TB-021",
        "tier": 0,
        "trace_id": "00481732284B6247",
        "question": (
            "For a read-only HDFS workload (instance 00481732284B6247), an operator "
            "wants a diagnostic check: is there any fault or anomaly affecting "
            "this request? If nothing appears wrong, say so explicitly and "
            "explain what you checked."
        ),
        "answer_required": ["no fault"],
        "answer_partial": ["no anomaly", "normal", "no issue", "healthy"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-022",
        "tier": 0,
        "trace_id": "000074229CD915F7",
        "question": (
            "For an RPC-heavy HDFS workload (instance 000074229CD915F7), an operator "
            "wants a diagnostic check: is there any fault or anomaly affecting "
            "this request? If nothing appears wrong, say so explicitly and "
            "explain what you checked."
        ),
        "answer_required": ["no fault"],
        "answer_partial": ["no anomaly", "normal", "no issue", "healthy"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-023",
        "tier": 0,
        "trace_id": "000016D5CDEDD7D9",
        "question": (
            "For a mixed read/write HDFS workload (instance 000016D5CDEDD7D9), an "
            "operator wants a diagnostic check: is there any fault or anomaly "
            "affecting this request? If nothing appears wrong, say so explicitly "
            "and explain what you checked."
        ),
        "answer_required": ["no fault"],
        "answer_partial": ["no anomaly", "normal", "no issue", "healthy"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-024",
        "tier": 0,
        "trace_id": "0072C2801761B9A3",
        "question": (
            "For a mixed read/write/RPC HDFS workload (instance 0072C2801761B9A3), an "
            "operator wants a diagnostic check: is there any fault or anomaly "
            "affecting this request? If nothing appears wrong, say so explicitly "
            "and explain what you checked."
        ),
        "answer_required": ["no fault"],
        "answer_partial": ["no anomaly", "normal", "no issue", "healthy"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-025",
        "tier": 0,
        "trace_id": "01E6916E4960ABDD",
        "question": (
            "For a write-only HDFS workload (instance 01E6916E4960ABDD), an operator "
            "wants a diagnostic check: is there any fault or anomaly affecting "
            "this request? If nothing appears wrong, say so explicitly and "
            "explain what you checked."
        ),
        "answer_required": ["no fault"],
        "answer_partial": ["no anomaly", "normal", "no issue", "healthy"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-026",
        "tier": 0,
        "trace_id": "0011036D9F8D905C",
        "question": (
            "For a read-only HDFS workload (instance 0011036D9F8D905C) running against "
            "a larger-than-usual cluster, an operator wants a diagnostic check: "
            "is there any fault or anomaly affecting this request? If nothing "
            "appears wrong, say so explicitly and explain what you checked."
        ),
        "answer_required": ["no fault"],
        "answer_partial": ["no anomaly", "normal", "no issue", "healthy"],
        "retrieval_signals": [],
    },
    {
        "case_id": "TB-027",
        "tier": 0,
        "trace_id": "00BD5474C3AB9F93",
        "question": (
            "For a mixed read/write HDFS workload (instance 00BD5474C3AB9F93) running "
            "against a larger-than-usual cluster, an operator wants a diagnostic "
            "check: is there any fault or anomaly affecting this request? If "
            "nothing appears wrong, say so explicitly and explain what you "
            "checked."
        ),
        "answer_required": ["no fault"],
        "answer_partial": ["no anomaly", "normal", "no issue", "healthy"],
        "retrieval_signals": [],
    },
]
