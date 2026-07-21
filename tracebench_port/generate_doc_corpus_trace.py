#!/usr/bin/env python3
"""
generate_doc_corpus_trace.py

Phase 3 of the TraceBench port: the TraceBench analog of
generate_doc_corpus.py. Produces two JSONL corpora:

  - data/doc_corpus_trace_perfault.jsonl   (PRIMARY — per-fault docs,
    matches the PaaS leakage posture)
  - data/doc_corpus_trace_category.jsonl   (SECONDARY — one doc per fault
    category, for the doc-leakage comparison in spec Phase 6.4)

Same JSONL record shape as generate_doc_corpus.py:
  {doc_id, doc_type, case_ids, components, failure_pattern, tier, title, content}
("case_ids" is generate_doc_corpus.py's "incident_ids", renamed to match this
port's TB-xxx case ids — same ground-truth-relevance role.)

Content discipline (same as the rest of this port — never invent a signal):
  - The MECHANISM (what the fault is, how HDFS behaves) is written from
    standard, well-documented Hadoop/HDFS architecture (DataNode/NameNode
    roles, the write pipeline, block reports, checksums, the deadNodes
    list) — the same kind of established-knowledge writing PaaS's docs do
    for Cloud Foundry internals.
  - The MANIFESTATION (quoted error text / latency numbers) is copied
    verbatim from the real evidence_anchor already verified in
    ground_truth_trace.json for that fault — never paraphrased into a
    different-looking string.

Run: python3 generate_doc_corpus_trace.py
"""
import json
import os

os.makedirs("./data", exist_ok=True)

perfault_docs = []
category_docs = []


def doc(target, doc_id, doc_type, case_ids, components, failure_pattern, tier, title, content):
    target.append({
        "doc_id": doc_id,
        "doc_type": doc_type,
        "case_ids": case_ids,
        "components": components,
        "failure_pattern": failure_pattern,
        "tier": tier,
        "title": title,
        "content": content.strip(),
    })


# ============================================================================
# CROSS-CUTTING ARCHITECTURE DOCS (perfault corpus only)
# ============================================================================

doc(
    perfault_docs,
    doc_id="arch_hdfs_write_pipeline",
    doc_type="architecture",
    case_ids=["TB-002", "TB-005", "TB-015", "TB-017", "TB-018", "TB-019"],
    components=["client", "datanode"],
    failure_pattern="architectural_reference",
    tier=1,
    title="HDFS Architecture: The Write Pipeline",
    content="""
# HDFS Architecture: The Write Pipeline

## Overview

When a client writes a block, it does not send the data to every replica in
parallel. Instead, the client and the replicas form a **pipeline**: the client
sends to the first DataNode, which forwards to the second, which forwards to
the third (for the default replication factor of 3), acknowledging back up
the chain once each stage confirms the write.

## Key stages an operation trace shows

1. **new blockSender / sendBlock** — a DataNode reads a stored block off disk
   and streams it to a requesting client or downstream DataNode (used for
   both client reads and pipeline replication).
2. **new BlockReceiver / receive block** — a DataNode accepts an incoming
   block from the client or an upstream DataNode in the pipeline, and writes
   it to local disk.
3. **verifiedByClient** — after a block is fully received, its checksum is
   verified against what the client computed. This step runs after the data
   transfer completes, so a slowdown here shows up as a delay *after* the
   write appears to finish, not during the transfer itself.

## Diagnostic Rule

If clients report **the write finished but something afterward felt slow**,
suspect `verifiedByClient` before suspecting the transfer itself — verification
is a distinct, later stage.

If a **specific DataNode** is consistently the slow link in the pipeline for
`new blockSender` / `new BlockReceiver`, suspect that node specifically, not
the client or the network path in general — compare its average duration for
the same operation against its peer DataNodes handling the same workload.
""",
)

doc(
    perfault_docs,
    doc_id="arch_datanode_liveness",
    doc_type="architecture",
    case_ids=["TB-011", "TB-012", "TB-013", "TB-016", "TB-020"],
    components=["client", "datanode", "namenode"],
    failure_pattern="architectural_reference",
    tier=1,
    title="HDFS Architecture: DataNode Liveness and the deadNodes List",
    content="""
# HDFS Architecture: DataNode Liveness and the deadNodes List

## Overview

A client reading or writing a block maintains a local **deadNodes** list for
the duration of an operation: once a DataNode fails to respond, the client
adds it to this list and routes subsequent requests for that block to a
different replica instead of retrying the failed node.

## Three distinct ways a DataNode can fail to respond

These produce genuinely different exception classes, and distinguishing them
from client-side evidence alone can be difficult — read carefully:

1. **The node's process is gone but the host is up.** The OS immediately
   resets the TCP connection attempt. Client sees:
   `java.net.ConnectException: Connection refused`.
   This happens whether the DataNode process was killed outright or the
   DataNode is dead/unresponsive at the process level — **the exception text
   alone does not distinguish "process was killed" from "process died" —
   both present as an immediate refusal.** Corroborating context (how many
   nodes are affected, whether the node was cleanly stopped vs. crashed, any
   preceding warning in NameNode block reports) is needed to tell them apart.
2. **The host itself is unreachable at the network layer** (routing/firewall
   issue, network partition). Client sees:
   `java.net.NoRouteToHostException: No route to host`.
   Like the refused-connection case above, this text alone does not
   distinguish a single misconfigured/disconnected node from a broader
   network-layer fault — both produce the identical exception string.
3. **The process is alive and the host is reachable, but the process never
   answers.** The client's request simply sits until its socket timeout
   (60000ms by default) expires. Client sees:
   `java.net.SocketTimeoutException: 60000 millis timeout while waiting for
   channel to be ready for read`.
   This is the one case that's reliably distinguishable from the other two by
   its symptom alone — it hangs for the full timeout window rather than
   failing immediately.

## Diagnostic Rule

`ConnectException` and `NoRouteToHostException` are both **immediate**
failures — the client moves on right away. `SocketTimeoutException` is a
**delayed** failure — the client waits out the full timeout first. If you can
only tell "immediate vs. delayed" apart from the trace, that's real
information; going further (distinguishing killed-process from dead-process,
or a single disconnected node from a network partition) requires more than
the exception text alone.
""",
)

doc(
    perfault_docs,
    doc_id="arch_checksums_and_corruption",
    doc_type="architecture",
    case_ids=["TB-003", "TB-007", "TB-008", "TB-009", "TB-010", "TB-014"],
    components=["datanode", "client"],
    failure_pattern="architectural_reference",
    tier=3,
    title="HDFS Architecture: Block Checksums and Corruption Signatures",
    content="""
# HDFS Architecture: Block Checksums and Corruption Signatures

## Overview

Every HDFS block is stored alongside a metadata (`.meta`) file that records
the checksum type and the checksum granularity (`bytesPerChecksum`) used to
verify the block's data. Corruption can hit either the block's **data** file
or its **metadata** file, and the two produce different symptoms:

## Data-file corruption

If the block's bytes themselves are altered (e.g. a bit flip on disk) but its
metadata is intact, the DataNode computes a checksum over the (corrupted)
data and compares it to the stored checksum. Mismatch produces:
`org.apache.hadoop.fs.ChecksumException: Checksum error: /blk_<id>:of:<path>`
This fails **after** the data has started transferring — the block is found,
opened, and read, and only the verification step catches the problem.

## Metadata-file corruption

If the `.meta` file itself is corrupted (its header fields — checksum type,
`bytesPerChecksum` — get scrambled), the DataNode cannot even construct a
valid checksum object to begin verifying the block. This fails **before**
any data is transferred, with an exception like:
`java.io.IOException: Could not create DataChecksum of type 3 with
bytesPerChecksum 50529027`
A giveaway that this is metadata corruption rather than a real configuration
value: real `bytesPerChecksum` values are small round numbers (512, 1024,
4096); a huge or type value outside HDFS's defined checksum types (e.g. type
49, or a byte count in the hundreds of millions) indicates the metadata
itself is garbled, not a genuine (if unusual) configuration.

## Block truncation / loss

Distinct from corruption: if part of the block's data is simply missing on
disk (truncated), a read fails with an offset/length mismatch:
`Offset 0 and length 67108864 don't match block blk_<id> ( blockLen 2201 )`
— note `blockLen` here is far smaller than the block size being requested.
If the DataNode's block scanner has already flagged the block as entirely
missing/invalid (rather than just short), the failure is more direct:
`Block blk_<id> is not valid.`

## Diagnostic Rule

- Fails **before** transfer, with implausible checksum-type/byte-count values
  → metadata corruption.
- Fails **during/after** transfer with a checksum mismatch on real data →
  data corruption.
- Fails with a length/offset mismatch or "not valid" → truncation or loss,
  not corruption of what's actually stored.
""",
)

doc(
    perfault_docs,
    doc_id="arch_latency_baselines",
    doc_type="architecture",
    case_ids=["TB-001", "TB-004", "TB-017", "TB-018", "TB-019"],
    components=["datanode"],
    failure_pattern="architectural_reference",
    tier=2,
    title="HDFS Architecture: Normal Operation Latency Baselines",
    content="""
# HDFS Architecture: Normal Operation Latency Baselines

## Overview

Per-operation latency in HDFS varies naturally with block size, network path,
and disk contention — there is no single fixed "normal" number. The
meaningful comparison is **relative**: how does one DataNode's average
duration for an operation compare to its peers handling the same workload at
the same time, or to the same operation's average under a verified
fault-free (normal) run.

## Real normal-condition averages (computed from verified fault-free traces)

| Operation | Average duration (normal traces) |
|---|---|
| writeBlock | ~6,396,738,815 ns |
| receiveBlock | ~6,351,146,366 ns |
| readBlock | ~2,438,328,629 ns |
| sendBlock | ~2,369,406,921 ns |
| OP: connect next Datanode | ~34,238,355 ns |
| OP: new BlockReceiver | ~30,335,965 ns |
| verifiedByClient | ~9,513,864 ns |
| OP: new blockSender | ~2,930,985 ns |

These are averaged across all DataNodes in verified normal-condition (no
injected fault) traces — use them as the cross-condition reference when no
single DataNode stands out as an outlier within the trace being investigated
(a cluster-wide, uniform slowdown).

## What "measurably slower" looks like in practice

Within a single trace, per-operation durations for the same op type across
different DataNodes are normally within a similar order of magnitude of each
other. When a single DataNode's average for one operation is several times
(commonly 2x-300x, depending on the fault) its peers' average for that same
operation within the same trace, that gap is the signal — not any absolute
threshold in milliseconds.

## Diagnostic Rule

1. **First check for a single outlier DataNode.** Compare one node's average
   duration for an operation against its peer DataNodes' average for the
   *same operation* in the *same trace*. A large, consistent gap points at
   that one node.
2. **If no single node stands out**, check whether the operation's average
   *across the whole trace* is elevated relative to genuinely normal
   (fault-free) traces for that same operation. A cluster-wide-but-uniform
   slowdown, with no single outlier node, points at a systemic rather than
   per-node cause.
3. Absence of any exception text does not mean absence of a fault — several
   fault types manifest purely as elevated latency with every request still
   eventually succeeding.
""",
)


# ============================================================================
# PER-FAULT DOCS (perfault corpus)
# One runbook / error_ref / config triple per distinct fault_name.
# ============================================================================

FAULTS = [
    dict(
        fault_name="killDN", category="Proc", tier=1, case_ids=["TB-013"],
        title_topic="DataNode Process Killed (killDN)",
        symptom="Read requests routed to a specific DataNode fail with an "
                "immediate connection refusal, and the client adds that node "
                "to its deadNodes list and retries against a different replica.",
        evidence_quote="IOException: java.net.ConnectException: Connection "
                        "refused: Failed to connect to /10.107.100.58:50010, "
                        "add to deadNodes and continue",
        mechanism="The DataNode process has been terminated (e.g. `kill` sent "
                   "to the process) while the host it runs on remains up. "
                   "The OS immediately resets any connection attempt to the "
                   "DataNode's port (50010) since nothing is listening there "
                   "anymore.",
        diagnosis_steps=[
            "Identify which DataNode the failing connection attempts target "
            "(the IP:port in the ConnectException).",
            "Confirm the refusal is immediate, not a timeout — this rules "
            "out suspendDN (which hangs) and points at either killDN or "
            "deadDN.",
            "Check how many distinct client sessions hit the same target "
            "node — a single consistently-refused node across many "
            "independent requests is the signature of a killed/dead process, "
            "not a transient blip.",
            "Note this signature is identical to deadDN's — see "
            "arch_datanode_liveness for what does and doesn't distinguish them.",
        ],
        resolution="Restart the DataNode process on the affected host. HDFS "
                    "replication means in-flight requests succeed against "
                    "other replicas in the meantime.",
        red_herrings=["The NameNode may not have marked the node dead yet — "
                      "clients discover the failure themselves per-request "
                      "before the NameNode's heartbeat timeout elapses."],
        config_params=[
            ("dfs.datanode.address", "0.0.0.0:50010", "The DataNode data-transfer port clients connect to."),
            ("dfs.client.socket-timeout", "60000 (ms)", "How long a client waits before giving up on a hung (not refused) connection."),
            ("dfs.namenode.heartbeat.recheck-interval", "300000 (ms)", "How long before the NameNode itself marks a silent DataNode dead — independent of what individual clients observe."),
        ],
    ),
    dict(
        fault_name="deadDN", category="Sys", tier=1, case_ids=["TB-011"],
        title_topic="DataNode Host/Process Dead (deadDN)",
        symptom="Read requests routed to a specific DataNode fail with an "
                "immediate connection refusal, and the client adds that node "
                "to its deadNodes list and retries against a different replica.",
        evidence_quote="IOException: java.net.ConnectException: Connection "
                        "refused: Failed to connect to /10.107.100.58:50010, "
                        "add to deadNodes and continue",
        mechanism="The DataNode is dead (process crashed or was force-stopped) "
                   "while its host remains reachable. As with a deliberately "
                   "killed process, nothing is listening on the DataNode port, "
                   "so connections are refused immediately rather than timing "
                   "out.",
        diagnosis_steps=[
            "Same first three steps as killDN — identify the target node, "
            "confirm immediate (not delayed) refusal, count affected client "
            "sessions.",
            "This fault's evidence signature is identical to killDN's in this "
            "dataset — see arch_datanode_liveness for the honest limits of "
            "what Description text alone can distinguish.",
        ],
        resolution="Investigate why the DataNode process is not running "
                    "(crash logs, OOM killer, disk failure) and restart it.",
        red_herrings=["Don't assume 'killed' vs 'crashed' from the exception "
                      "text alone — both look the same from the client side."],
        config_params=[
            ("dfs.datanode.address", "0.0.0.0:50010", "The DataNode data-transfer port clients connect to."),
            ("dfs.namenode.heartbeat.recheck-interval", "300000 (ms)", "How long before the NameNode marks a silent DataNode dead."),
        ],
    ),
    dict(
        fault_name="panicDN", category="Sys", tier=1, case_ids=["TB-016"],
        title_topic="DataNode Network-Unreachable (panicDN)",
        symptom="Read requests routed to a specific DataNode fail immediately "
                "with a network-level routing error, distinct from a refused "
                "connection.",
        evidence_quote="IOException: java.net.NoRouteToHostException: No "
                        "route to host: Failed to connect to "
                        "/10.107.100.60:50010, add to deadNodes and continue",
        mechanism="The DataNode's host is unreachable at the network layer — "
                   "the routing table or firewall state prevents the client's "
                   "packets from ever reaching the host at all, rather than "
                   "the host actively refusing the connection.",
        diagnosis_steps=[
            "Confirm the exception is NoRouteToHostException, not "
            "ConnectException — this is a network-layer failure, not a "
            "process-layer one.",
            "This signature is identical to disconnectDN's — see "
            "arch_datanode_liveness; corroborate with how many nodes are "
            "affected simultaneously before concluding single-node vs. "
            "broader network fault.",
        ],
        resolution="Investigate network routing/firewall state between the "
                    "client subnet and the affected DataNode's host.",
        red_herrings=["This is not a process problem — restarting the "
                      "DataNode process will not fix a network routing issue."],
        config_params=[
            ("dfs.datanode.address", "0.0.0.0:50010", "The DataNode data-transfer port; unreachable here means a network-layer problem, not an application one."),
        ],
    ),
    dict(
        fault_name="suspendDN", category="Proc", tier=1, case_ids=["TB-020"],
        title_topic="DataNode Process Suspended/Hung (suspendDN)",
        symptom="Read requests routed to a specific DataNode do not fail "
                "immediately — they hang until the client's socket timeout "
                "expires, then the client gives up on that node.",
        evidence_quote="IOException: java.net.SocketTimeoutException: 60000 "
                        "millis timeout while waiting for channel to be ready "
                        "for read. ch : java.nio.channels.SocketChannel"
                        "[connected local=/10.107.100.123:42282 "
                        "remote=/10.107.100.62:50010]: Failed to connect to "
                        "/10.107.100.62",
        mechanism="The DataNode process is alive and its port is open (the "
                   "TCP connection itself succeeds — note 'connected' in the "
                   "channel description), but the process never responds to "
                   "the request. The client waits the full socket-timeout "
                   "window before giving up.",
        diagnosis_steps=[
            "Confirm the failure takes ~60 seconds (dfs.client.socket-timeout) "
            "to surface, rather than being immediate — this is the "
            "distinguishing signature vs. killDN/deadDN/panicDN/disconnectDN.",
            "A connected-but-unresponsive process (as opposed to refused or "
            "unreachable) suggests the process itself is suspended/paused "
            "(e.g. STOP signal, GC pause, thread starvation) rather than "
            "dead or network-isolated.",
        ],
        resolution="Identify why the DataNode process stopped responding "
                    "(check for a suspend/pause signal, long GC pause, or "
                    "deadlock) and resume or restart it.",
        red_herrings=["The connection itself succeeding can look like the "
                      "node is healthy at first glance — the hang only shows "
                      "up once the client tries to actually read data."],
        config_params=[
            ("dfs.client.socket-timeout", "60000 (ms)", "How long a client waits on a connected-but-unresponsive DataNode before giving up — matches the ~60s delay in this fault's evidence."),
        ],
    ),
    dict(
        fault_name="readOnlyDN", category="Sys", tier=1, case_ids=["TB-017"],
        title_topic="DataNode Forced Read-Only (readOnlyDN)",
        symptom="No exceptions are raised, but the block-receive step for "
                "write operations is measurably slower on one DataNode than "
                "its peers handling the same workload.",
        evidence_quote="datanode027 averages 39,022,155ns for OP: new "
                        "BlockReceiver across this trace_set vs 17,688,923ns "
                        "for its peer datanodes (2.2x)",
        mechanism="The DataNode's underlying storage has been forced "
                   "read-only. Rather than failing outright, incoming block "
                   "writes to this node are delayed/degraded in the receive "
                   "path, since the node cannot commit the write to disk the "
                   "way it normally would.",
        diagnosis_steps=[
            "Look for elevated 'new BlockReceiver' / 'receive block' "
            "duration on one node during a write workload, with no "
            "corresponding exception text.",
            "This is a purely latency-based signature — do not expect any "
            "Description text evidence for this fault.",
            "Confirm the workload is write-oriented; a read-only node has no "
            "reason to show up in read-path operations.",
        ],
        resolution="Check the DataNode's local disk/volume mount state and "
                    "restore write access.",
        red_herrings=["Because nothing throws an exception, this fault is "
                      "easy to miss if only checking for errors — the "
                      "diagnostic signal is purely comparative latency."],
        config_params=[
            ("dfs.datanode.data.dir", "(local volume paths)", "The DataNode's storage directories; a read-only mount here degrades the write path without raising an HDFS-level exception."),
        ],
    ),
    dict(
        fault_name="slowDN", category="Net", tier=2, case_ids=["TB-018"],
        title_topic="Single Slow DataNode (slowDN)",
        symptom="Every request still eventually succeeds, but one specific "
                "DataNode is measurably slower than its peers across "
                "multiple block-transfer operations.",
        evidence_quote="datanode046 averages 58,519,845ns for verifiedByClient "
                        "across this trace_set vs 178,178ns for its peer "
                        "datanodes (328.4x)",
        mechanism="An artificial network delay has been injected on this "
                   "DataNode's traffic (e.g. via a traffic-shaping rule), "
                   "slowing every operation that touches it without causing "
                   "any operation to actually fail.",
        diagnosis_steps=[
            "Compare each DataNode's average duration for the same operation "
            "within the trace — a single node with a dramatically higher "
            "average, while every request still completes, is this fault's "
            "signature (see arch_latency_baselines).",
            "Distinguish from readOnlyDN by workload: slowDN's delay shows "
            "up broadly across operation types (read and write paths), not "
            "narrowly on the write-receive path only.",
            "Distinguish from slowHDFS: here only one node is affected, not "
            "the whole cluster.",
        ],
        resolution="Investigate the network path to the specific affected "
                    "DataNode (interface errors, congestion, misconfigured "
                    "QoS/shaping rules).",
        red_herrings=["Nothing fails, so this can look like normal variance "
                      "at a glance — check the magnitude of the gap against "
                      "peer nodes before dismissing it."],
        config_params=[
            ("dfs.client.socket-timeout", "60000 (ms)", "The ceiling before a slow node would eventually be treated as failed outright — a slowdown well under this still succeeds, just slowly."),
        ],
    ),
    dict(
        fault_name="slowHDFS", category="Net", tier=2, case_ids=["TB-019"],
        title_topic="Cluster-Wide Slowdown (slowHDFS)",
        symptom="Block-receive operations across the cluster run somewhat "
                "slower than the normal-condition baseline overall, without "
                "any single DataNode standing out as dramatically worse than "
                "the rest.",
        evidence_quote="datanode019 averages 161,798,467ns for OP: new "
                        "BlockReceiver across this trace_set vs 114,794,868ns "
                        "for its peer datanodes (1.4x)",
        mechanism="A network-level delay has been injected broadly (e.g. at "
                   "a shared network segment or switch), rather than targeting "
                   "one DataNode. Every node is affected roughly equally, so "
                   "no single host is an outlier relative to its peers within "
                   "the same trace.",
        diagnosis_steps=[
            "Check for a per-node outlier first (slowDN's signature) — if "
            "none exists but the operation's overall average across the "
            "trace is still elevated relative to a genuinely normal "
            "baseline for that operation, suspect a cluster-wide cause.",
            "This is the subtlest of the latency faults — the gap vs. a true "
            "normal baseline can be small (the admitted case here shows only "
            "a 1.4x ratio), so don't expect a dramatic multiple.",
        ],
        resolution="Investigate shared network infrastructure (switches, "
                    "uplinks) rather than any individual DataNode.",
        red_herrings=["Don't chase individual DataNodes here — by "
                      "definition, no single node's ratio to its peers will "
                      "look abnormal for this fault; the comparison that "
                      "matters is trace-wide vs. a genuinely normal run."],
        config_params=[],
    ),
    dict(
        fault_name="disconnectDN", category="Net", tier=2, case_ids=["TB-012"],
        title_topic="DataNode Disconnected from Network (disconnectDN)",
        symptom="Read requests routed to a specific DataNode fail immediately "
                "with a network-level routing error, and the client marks "
                "that node unreachable for the rest of the operation.",
        evidence_quote="IOException: java.net.NoRouteToHostException: No "
                        "route to host: Failed to connect to "
                        "/10.107.100.64:50010, add to deadNodes and continue",
        mechanism="The DataNode has been disconnected from the network (its "
                   "network interface or link is down), so packets destined "
                   "for it are dropped at the routing layer before ever "
                   "reaching the host.",
        diagnosis_steps=[
            "Same as panicDN — confirm NoRouteToHostException, not "
            "ConnectException.",
            "This fault propagates further than a simple process failure: a "
            "disconnected node also stops participating in replication and "
            "block reports, which can surface as broader cluster-health "
            "symptoms beyond the immediate client-facing exception.",
        ],
        resolution="Check the physical/virtual network interface state for "
                    "the affected host.",
        red_herrings=["Identical client-facing exception to panicDN — the "
                      "distinction (single transient node vs. a node fully "
                      "cut off from cluster participation) requires looking "
                      "beyond a single client's error text."],
        config_params=[],
    ),
    dict(
        fault_name="corruptBlk", category="Data", tier=3, case_ids=["TB-007"],
        title_topic="Block Data Corruption (corruptBlk)",
        symptom="A read otherwise proceeds normally — the block is found and "
                "data begins transferring — but fails partway with a checksum "
                "mismatch on the actual data.",
        evidence_quote="IOException: org.apache.hadoop.fs.ChecksumException: "
                        "Checksum error: /blk_-5596974342584343053:of:"
                        "/user/hadoop/hdfsFile_2_blocks at 0",
        mechanism="The block's data file on disk has been altered (bit "
                   "corruption), but its metadata (.meta) file — the "
                   "checksum type and granularity — is intact. See "
                   "arch_checksums_and_corruption for the data-vs-metadata "
                   "distinction.",
        diagnosis_steps=[
            "Confirm the failure is a ChecksumException specifically, "
            "occurring after the block has started transferring — this "
            "rules out metadata corruption (which fails before transfer).",
            "The `at 0` offset in the exception indicates where in the block "
            "the checksum mismatch was detected.",
        ],
        resolution="The corrupted replica should be reported to the "
                    "NameNode's corrupt-block tracking and re-replicated "
                    "from a healthy replica.",
        red_herrings=["The read initially looks like it's succeeding (block "
                      "found, transfer started) — the corruption only "
                      "surfaces once enough data has been read to fail "
                      "verification."],
        config_params=[
            ("dfs.bytes-per-checksum", "512", "Granularity at which HDFS verifies data against its stored checksum."),
        ],
    ),
    dict(
        fault_name="corruptMeta", category="Data", tier=3, case_ids=["TB-008"],
        title_topic="Block Metadata Corruption (corruptMeta)",
        symptom="A read fails immediately, before any block data is "
                "transferred, with an invalid checksum-configuration error.",
        evidence_quote="IOException: java.io.IOException: Could not create "
                        "DataChecksum of type 3 with bytesPerChecksum "
                        "50529027",
        mechanism="The block's .meta file — which stores the checksum type "
                   "and bytesPerChecksum values used to verify that block — "
                   "has itself been corrupted. The DataNode cannot even "
                   "construct a valid checksum object to begin verification, "
                   "so the read fails before any data transfer starts. See "
                   "arch_checksums_and_corruption.",
        diagnosis_steps=[
            "Note the implausible bytesPerChecksum value (real values are "
            "512/1024/4096, not values in the tens of millions) — this is "
            "the giveaway that the metadata itself is garbled, not that "
            "someone configured an unusual (but valid) checksum granularity.",
            "Confirm this fails before any data transfer, unlike "
            "corruptBlk's mid-transfer checksum mismatch.",
        ],
        resolution="The block's replica (data + metadata) should be marked "
                    "corrupt and re-replicated from a healthy replica; the "
                    ".meta file cannot be repaired in place.",
        red_herrings=["Don't mistake the nonsensical checksum-type/byte "
                      "values for a real (if unusual) cluster configuration "
                      "— HDFS's defined checksum types are small integers, "
                      "not arbitrary large numbers."],
        config_params=[
            ("dfs.checksum.type", "CRC32C", "The default checksum algorithm; a valid type is a small enumerated value, not an arbitrary integer."),
        ],
    ),
    dict(
        fault_name="cutBlk", category="Data", tier=3, case_ids=["TB-009"],
        title_topic="Block Data Truncated (cutBlk)",
        symptom="A read fails because the block on disk is much shorter than "
                "its recorded length, as if part of the block's data is "
                "simply missing.",
        evidence_quote="IOException: java.io.IOException:  Offset 0 and "
                        "length 67108864 don't match block "
                        "blk_2749751980111740114_1415 ( blockLen 2201 )",
        mechanism="Part of the block's data file has been removed from disk "
                   "(truncation), while the DataNode's in-memory/metadata "
                   "record of the block's expected length is unchanged. A "
                   "read requesting the full recorded length finds far less "
                   "data actually present.",
        diagnosis_steps=[
            "Compare the requested offset/length in the exception to the "
            "reported `blockLen` — a `blockLen` far smaller than the "
            "requested length confirms truncation rather than corruption of "
            "existing bytes.",
            "Distinguish from corruptBlk: corruption produces a checksum "
            "mismatch on data that IS present; truncation produces a length "
            "mismatch because data is simply absent.",
        ],
        resolution="Re-replicate the block from a healthy replica; the "
                    "truncated local copy cannot be repaired.",
        red_herrings=[],
        config_params=[],
    ),
    dict(
        fault_name="cutMeta", category="Data", tier=3, case_ids=["TB-010"],
        title_topic="Block Metadata Truncated (cutMeta)",
        symptom="A read fails immediately with implausible checksum-"
                "configuration values — a checksum type and byte-count that "
                "don't correspond to any valid HDFS configuration.",
        evidence_quote="IOException: java.io.IOException: Could not create "
                        "DataChecksum of type 49 with bytesPerChecksum "
                        "825307441",
        mechanism="The block's .meta file has been truncated (part of it "
                   "removed), so the DataNode reads garbage or "
                   "partially-missing bytes where the checksum type and "
                   "bytesPerChecksum header fields should be. See "
                   "arch_checksums_and_corruption.",
        diagnosis_steps=[
            "Same giveaway as corruptMeta — nonsensical type (here 49, "
            "outside HDFS's defined checksum types) and byte-count values.",
            "Distinguishing cutMeta from corruptMeta by symptom alone is not "
            "reliable — both produce the same class of 'Could not create "
            "DataChecksum' failure with implausible parameters; treat both "
            "as 'this block's metadata is unusable' and proceed the same "
            "way operationally.",
        ],
        resolution="Mark the replica corrupt and re-replicate from a "
                    "healthy copy.",
        red_herrings=[],
        config_params=[],
    ),
    dict(
        fault_name="lossBlk", category="Data", tier=3, case_ids=["TB-014"],
        title_topic="Block Data Lost/Invalidated (lossBlk)",
        symptom="A read fails because the DataNode reports that the "
                "requested block simply isn't valid or present on it, even "
                "though the read was routed there.",
        evidence_quote="IOException: java.io.IOException: Block "
                        "blk_-1603825720303258975_1176 is not valid.",
        mechanism="The block's data has been removed entirely from this "
                   "DataNode (or the DataNode's block scanner has already "
                   "invalidated it), so there's nothing to even attempt "
                   "reading — this is a more complete failure than "
                   "truncation, where at least some data remains.",
        diagnosis_steps=[
            "The 'is not valid' phrasing (vs. an offset/length mismatch or "
            "checksum error) indicates the DataNode has no usable copy at "
            "all, not a partial/corrupted one.",
            "Check whether the NameNode's replica count for this block has "
            "already dropped — this fault's effect is equivalent to losing "
            "a replica outright.",
        ],
        resolution="Confirm other replicas are healthy and let HDFS's "
                    "replication monitor restore the target replication "
                    "factor; if all replicas are affected, restore from "
                    "backup.",
        red_herrings=[],
        config_params=[
            ("dfs.replication", "3", "Default replication factor — determines how many other copies exist to serve reads when one replica is lost."),
        ],
    ),
    dict(
        fault_name="lossMeta", category="Data", tier=3, case_ids=["TB-015"],
        title_topic="Block Metadata Lost (lossMeta)",
        symptom="No exception is raised, but the block-verification step "
                "after transfer is taking drastically longer to complete on "
                "this trace than it does elsewhere.",
        evidence_quote="datanode048 averages 33,456,252ns for verifiedByClient "
                        "across this trace_set vs 168,563ns for its peer "
                        "datanodes (198.5x)",
        mechanism="The block's metadata has been lost or is being "
                   "reconstructed/regenerated rather than cleanly absent, "
                   "which manifests here as a dramatic slowdown in the "
                   "post-transfer verification step rather than an outright "
                   "failure — the operation still eventually succeeds, just "
                   "far more slowly.",
        diagnosis_steps=[
            "Look for elevated 'verifiedByClient' duration on one node, "
            "with no corresponding exception — this fault does not always "
            "manifest as a hard error the way corruptMeta/cutMeta do.",
            "This is a latency-only signature; treat it the same way as "
            "readOnlyDN/slowDN — compare against peer DataNodes' average "
            "for the same operation within the same trace.",
        ],
        resolution="Investigate the block's metadata file state directly on "
                    "the affected DataNode's storage.",
        red_herrings=["Because the operation still succeeds, this is easy "
                      "to dismiss as normal variance — check the magnitude "
                      "of the gap (order-of-magnitude, not a few percent) "
                      "before ruling it out."],
        config_params=[],
    ),
    dict(
        fault_name="Mul_AA", category="AA", tier=3, case_ids=["TB-001"],
        title_topic="Multi-Fault Combination (AnarchyApe)",
        symptom="Some read/write/RPC requests take noticeably longer than "
                "others, though nothing fails outright — evidenced by "
                "elevated block-transfer duration on a specific DataNode.",
        evidence_quote="datanode003 averages 84,461,227ns for OP: new "
                        "blockSender across this trace_set vs 3,060,219ns "
                        "for its peer datanodes (27.6x)",
        mechanism="Multiple simultaneous fault conditions have been injected "
                   "together across the cluster (an 'AnarchyApe'-style chaos "
                   "run combining several individual fault types), rather "
                   "than a single isolated cause. The observable effect can "
                   "still localize to one node's elevated latency for a "
                   "given operation, but the underlying cause may be more "
                   "than one simultaneous condition.",
        diagnosis_steps=[
            "Treat the initial signal the same as any single-node latency "
            "outlier (see arch_latency_baselines) — but don't assume a "
            "single root cause once localized; check for additional "
            "unrelated symptoms elsewhere in the same trace before "
            "concluding the investigation.",
            "Mixed read/write/RPC workload context is a hint this may be a "
            "combination run rather than an isolated single-fault "
            "condition.",
        ],
        resolution="Because multiple conditions may be present "
                    "simultaneously, resolving the first-found symptom may "
                    "not fully resolve the incident — re-verify after each "
                    "fix.",
        red_herrings=["Don't stop investigating after finding one plausible "
                      "cause in a mixed/combination workload — there may be "
                      "more than one."],
        config_params=[],
    ),
    dict(
        fault_name="Sin_Bug", category="Bug", tier=3, case_ids=["TB-002"],
        title_topic="Real Hadoop Bug Reproduction (Sin_Bug)",
        symptom="Write operations complete, but the client-side block "
                "verification step after some writes takes dramatically "
                "longer than expected — clients appear to hang briefly right "
                "after finishing a block write.",
        evidence_quote="datanode003 averages 1,051,558,172ns for "
                        "verifiedByClient across this trace_set vs 908,410ns "
                        "for its peer datanodes (1157.6x)",
        mechanism="This fault reproduces a known real-world Hadoop defect "
                   "(rather than an artificially injected condition), "
                   "specifically affecting the post-write verification "
                   "path. Because it's a genuine software bug rather than a "
                   "clean fault injection, its trigger conditions can be "
                   "narrower/more workload-specific than the synthetic "
                   "faults.",
        diagnosis_steps=[
            "The extreme ratio here (over 1000x peer average) is "
            "characteristic of a pathological code path being hit, not just "
            "ordinary contention — a smaller multiple would be more "
            "consistent with an injected slowdown.",
            "Confirm the delay is specifically in verifiedByClient (a "
            "post-write step), not in the transfer itself.",
        ],
        resolution="This corresponds to a known Hadoop issue in the "
                    "verification path; the practical fix is the software "
                    "patch/version that addresses it, not a runtime "
                    "configuration change.",
        red_herrings=["Because this is a real bug and not an artificial "
                      "fault, it may only reproduce under the specific "
                      "combined workload conditions present here — don't "
                      "expect it to generalize the way a simple 'slow node' "
                      "fault would."],
        config_params=[],
    ),
    dict(
        fault_name="Sin_Data", category="Data", tier=3, case_ids=["TB-003"],
        title_topic="Data-Category Combination Fault (Sin_Data)",
        symptom="A read fails immediately with a checksum-configuration "
                "error before any data is transferred, as if the block's "
                "checksum metadata itself is malformed.",
        evidence_quote="IOException: java.io.IOException: Could not create "
                        "DataChecksum of type 3 with bytesPerChecksum "
                        "50529027",
        mechanism="A Data-category fault condition (metadata-level "
                   "corruption, per arch_checksums_and_corruption) was "
                   "injected as part of a combined-workload run. The "
                   "resulting evidence signature is the same "
                   "metadata-corruption pattern seen in the isolated "
                   "corruptMeta fault.",
        diagnosis_steps=[
            "Apply the same reasoning as corruptMeta: implausible checksum "
            "type/byte-count values, failure before any transfer begins.",
            "The mixed read/write/RPC workload context is the main hint "
            "this is a combination-run case rather than an isolated single "
            "fault.",
        ],
        resolution="Same as corruptMeta — mark the replica corrupt and "
                    "re-replicate.",
        red_herrings=[],
        config_params=[],
    ),
    dict(
        fault_name="Sin_Net", category="Net", tier=3, case_ids=["TB-004"],
        title_topic="Network-Category Combination Fault (Sin_Net)",
        symptom="Requests succeed, but one specific DataNode responds to "
                "block-transfer requests much more slowly than the rest of "
                "the cluster.",
        evidence_quote="datanode046 averages 27,658,900ns for OP: new "
                        "blockSender across this trace_set vs 1,269,153ns "
                        "for its peer datanodes (21.8x)",
        mechanism="A Net-category fault condition (a targeted network "
                   "slowdown, the same class as slowDN) was injected as "
                   "part of a combined-workload run.",
        diagnosis_steps=[
            "Apply the same reasoning as slowDN: single-node outlier, "
            "compare against peer DataNode averages for the same operation.",
        ],
        resolution="Same as slowDN — investigate the network path to the "
                    "specific affected DataNode.",
        red_herrings=[],
        config_params=[],
    ),
    dict(
        fault_name="Sin_Proc", category="Proc", tier=3, case_ids=["TB-005"],
        title_topic="Process-Category Combination Fault (Sin_Proc)",
        symptom="The block-verification step following writes is "
                "intermittently slow — most writes verify quickly but a "
                "subset take far longer than the rest.",
        evidence_quote="datanode033 averages 92,702,267ns for "
                        "verifiedByClient across this trace_set vs "
                        "6,892,375ns for its peer datanodes (13.4x)",
        mechanism="A Process-category fault condition was injected as part "
                   "of a combined-workload run, affecting the same "
                   "post-write verification path lossMeta and Sin_Bug also "
                   "manifest through.",
        diagnosis_steps=[
            "Apply the same reasoning as lossMeta: elevated "
            "verifiedByClient duration on one node, no corresponding "
            "exception.",
        ],
        resolution="Investigate the affected DataNode's process health "
                    "(the same category of cause as suspendDN/killDN, here "
                    "manifesting as degradation rather than outright "
                    "failure).",
        red_herrings=[],
        config_params=[],
    ),
    dict(
        fault_name="Sin_Sys", category="Sys", tier=3, case_ids=["TB-006"],
        title_topic="System-Category Combination Fault (Sin_Sys)",
        symptom="Some requests fail outright with a connection-refused "
                "error when trying to reach specific DataNodes, while other "
                "requests complete normally.",
        evidence_quote="IOException: java.net.ConnectException: Connection "
                        "refused: failed to connect to 10.107.100.96:50010",
        mechanism="A System-category fault condition (the same class as "
                   "killDN/deadDN) was injected as part of a combined-"
                   "workload run, affecting specific DataNodes while leaving "
                   "others untouched.",
        diagnosis_steps=[
            "Apply the same reasoning as killDN/deadDN: immediate refusal, "
            "identify the affected node(s).",
            "Note that in a combination run, only some requests are "
            "affected — don't conclude the whole cluster is impacted just "
            "because some requests fail.",
        ],
        resolution="Same as killDN/deadDN — restart or investigate the "
                    "affected DataNode process(es).",
        red_herrings=[],
        config_params=[],
    ),
]


def build_fault_triple(f):
    fault = f["fault_name"]
    steps = "\n".join(f"{i+1}. {s}" for i, s in enumerate(f["diagnosis_steps"]))
    red_herrings = (
        "\n\n## Red Herrings\n" + "\n".join(f"- {r}" for r in f["red_herrings"])
        if f["red_herrings"] else ""
    )
    runbook_content = f"""
# Runbook: {f['title_topic']}

## Symptom
{f['symptom']}

## Diagnosis Steps

{steps}

## Resolution
{f['resolution']}
{red_herrings}
"""
    doc(perfault_docs, f"runbook_{fault}", "runbook", f["case_ids"],
        ["client", "datanode"], fault, f["tier"],
        f"Runbook: {f['title_topic']}", runbook_content)

    error_ref_content = f"""
# Error Reference: {f['title_topic']}

## Log Message

```
{f['evidence_quote']}
```

## Meaning
{f['mechanism']}

## Symptom Observed
{f['symptom']}
"""
    doc(perfault_docs, f"error_ref_{fault}", "error_ref", f["case_ids"],
        ["client", "datanode"], fault, f["tier"],
        f"Error Reference: {f['title_topic']}", error_ref_content)

    if f["config_params"]:
        rows = "\n".join(
            f"| `{name}` | {default} | {meaning} |"
            for name, default, meaning in f["config_params"]
        )
        config_content = f"""
# Configuration Reference: {f['title_topic']}

| Parameter | Default | Relevance |
|---|---|---|
{rows}
"""
    else:
        config_content = f"""
# Configuration Reference: {f['title_topic']}

No HDFS-level configuration parameter directly governs this condition — it
reflects the injected fault itself (process/network/data state) rather than
a tunable setting.
"""
    doc(perfault_docs, f"config_{fault}", "config", f["case_ids"],
        ["client", "datanode"], fault, f["tier"],
        f"Configuration Reference: {f['title_topic']}", config_content)


for f in FAULTS:
    build_fault_triple(f)


# ============================================================================
# CATEGORY-ONLY CORPUS (secondary, spec 3.3)
# One doc per fault_category, collapsing per-fault detail.
# ============================================================================

CATEGORY_SUMMARY = {
    "Proc": (
        1,
        "Process-Level Faults",
        "A DataNode's process is killed, suspended, or hung. Symptoms range "
        "from an immediate connection refusal (process gone) to a full "
        "socket-timeout hang (process alive but unresponsive). Affected "
        "DataNodes stop serving requests; other replicas continue normally.",
    ),
    "Sys": (
        1,
        "System-Level Faults",
        "A DataNode's host becomes dead, network-unreachable, or forced "
        "read-only. Symptoms include connection refusal, network routing "
        "errors, or (for read-only) a pure latency degradation on the write "
        "path with no exception at all.",
    ),
    "Net": (
        2,
        "Network-Level Faults",
        "Network conditions between clients/DataNodes are degraded: a "
        "specific node disconnected (routing failure), a specific node "
        "slowed (latency injection), or the whole cluster's network path "
        "uniformly slowed. Distinguish single-node vs. cluster-wide by "
        "comparing one DataNode's average against its peers within the "
        "same trace.",
    ),
    "Data": (
        3,
        "Data-Level Faults",
        "A block's data or metadata is corrupted, truncated, or lost. "
        "Metadata corruption fails before any transfer (implausible "
        "checksum values); data corruption fails mid-transfer (checksum "
        "mismatch on real data); truncation/loss fails with a length "
        "mismatch or an explicit 'not valid' block state.",
    ),
    "Bug": (
        3,
        "Real Hadoop Bug Reproductions",
        "Genuine (not synthetically injected) Hadoop defects reproduced "
        "under specific workload conditions, most often surfacing as "
        "extreme, pathological latency in a particular operation rather "
        "than a clean single-cause symptom.",
    ),
    "AA": (
        3,
        "Multi-Fault Combination (AnarchyApe)",
        "Several fault conditions injected simultaneously. Symptoms can "
        "still localize to a single component, but more than one condition "
        "may be present — don't stop investigating after the first "
        "plausible cause.",
    ),
}

CATEGORY_CASE_IDS = {}
for f in FAULTS:
    CATEGORY_CASE_IDS.setdefault(f["category"], []).extend(f["case_ids"])

for category, (tier, title, summary) in CATEGORY_SUMMARY.items():
    doc(
        category_docs,
        doc_id=f"category_{category}",
        doc_type="category_overview",
        case_ids=CATEGORY_CASE_IDS.get(category, []),
        components=["client", "datanode"],
        failure_pattern=category,
        tier=tier,
        title=f"Fault Category: {title}",
        content=f"""
# Fault Category: {title}

{summary}
""",
    )


# ============================================================================
# WRITE OUTPUT
# ============================================================================

with open("./data/doc_corpus_trace_perfault.jsonl", "w", encoding="utf-8") as fh:
    for d in perfault_docs:
        fh.write(json.dumps(d) + "\n")
print(f"Wrote {len(perfault_docs)} docs -> data/doc_corpus_trace_perfault.jsonl")

with open("./data/doc_corpus_trace_category.jsonl", "w", encoding="utf-8") as fh:
    for d in category_docs:
        fh.write(json.dumps(d) + "\n")
print(f"Wrote {len(category_docs)} docs -> data/doc_corpus_trace_category.jsonl")
