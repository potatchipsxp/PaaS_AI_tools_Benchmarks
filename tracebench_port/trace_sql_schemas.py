"""
trace_sql_schemas.py

Schema-description strings for the two TraceBench SQL-agent tracks, meant to
be pasted into diagnostic_agent.py's SQL_SCHEMA_DESCRIPTION / SQL_INCLUDE_TABLES
CONFIG constants (and sql_agent.py's own DB_URI/INCLUDE_TABLES for standalone
smoke-testing) -- same "edit CONFIG, then run" workflow already used for
model-tier sweeps.

Track A (flat `logs`) and Track B (native Event/Edge/Trace/Operation) point at
the SAME data in benchmark_trace_db.sqlite; only the table shape differs.
This is the ONE sanctioned tool-surface change for the whole port (Phase 4).
"""

TRACK_A_SCHEMA_DESCRIPTION = """
DATABASE: HDFS request/operation logs, flattened from TraceBench traces into
a PaaS-style flat log table (Track A -- "flatten" arm of the port).

TABLE: logs
  row_uuid      TEXT  -- unique row identifier
  timestamp     TEXT  -- ISO-8601. NOTE: TraceBench's raw StartTime is a
                          per-host relative clock reading, not a shared
                          wall-clock epoch, so this timestamp is anchored per
                          (request, host) to the trace's real collection date
                          -- it preserves correct ordering of events on the
                          SAME host/request, but is NOT meaningful for
                          claiming two DIFFERENT hosts' events happened at
                          precisely the same instant.
  source_system TEXT  -- always 'hdfs_tracebench'
  component     TEXT  -- the HDFS operation name (OpName), e.g. 'readBlock',
                          'sendBlock', 'writeBlock', 'RPC:getFileInfo',
                          'fs -copyToLocal', 'OP: new BlockReceiver'
  subcomponent  TEXT  -- the executing code path/agent, e.g. 'Namenode',
                          'RPC Client', 'DataXceiver' (varies by node role)
  level         TEXT  -- 'ERROR' (message contains a real exception string),
                          'WARN' (this node/operation is a measured latency
                          outlier vs its peers for this request's workload),
                          or 'INFO' (normal completion)
  node_id       TEXT  -- the host that executed this operation, e.g.
                          'datanode012', 'namenode', 'client024'
  instance_id   TEXT  -- the HDFS request/session this event belongs to (what
                          an operator would call "this instance" of a
                          workload) -- always filter to ONE instance_id when
                          diagnosing a specific reported problem
  event_type    TEXT  -- coarse bucket: 'read', 'write', 'rpc',
                          'client_command', 'error', or 'other'
  message       TEXT  -- the real operation outcome text -- either
                          'Success: ...' or an exception string
                          (e.g. 'IOException: java.net.ConnectException:
                          Connection refused: ...')
  thread_id     TEXT  -- the executing thread on that host (repeats across
                          many events on the same thread -- group by this to
                          see one thread's full sequence of operations)
  block_id      TEXT  -- always NULL (block identifiers, where present,
                          already appear inline in message text)
  source_file   TEXT  -- always NULL

ONLY ONE TABLE EXISTS: logs. Do not reference any other table name.
"""

TRACK_B_SCHEMA_DESCRIPTION = """
DATABASE: HDFS distributed traces (TraceBench), native structure (Track B --
"trace-native" arm of the port). Four tables reconstruct the actual call
tree and timing of HDFS requests; nothing is flattened.

TABLE: Event
  TaskID      TEXT -- the HDFS request/session this event belongs to (what an
                       operator would call "this instance" of a workload)
  TID         TEXT -- the executing thread on HostName. NOT a unique
                       per-event id -- the same TID repeats across every
                       event that thread performs. Use (TaskID, TID,
                       StartTime) together to pick out one specific event.
  OpName      TEXT -- the HDFS operation name, e.g. 'readBlock', 'sendBlock',
                       'RPC:getFileInfo', 'OP: new BlockReceiver'
  StartTime   INTEGER -- nanosecond-resolution clock reading, LOCAL TO
                       HostName. Only comparable to other StartTime/EndTime
                       values on the SAME HostName -- do NOT subtract or
                       compare StartTime across two different HostName
                       values, the results are meaningless (different hosts
                       use independent clock origins).
  EndTime     INTEGER -- same clock as StartTime; (EndTime - StartTime) on
                       ONE row is always a valid duration in nanoseconds,
                       regardless of host.
  HostAddress TEXT -- IP address that executed this event
  HostName    TEXT -- hostname that executed this event, e.g. 'datanode012',
                       'namenode', 'client024'
  Agent       TEXT -- executing code path, e.g. 'Namenode', 'RPC Client'
  Description TEXT -- the real operation outcome: 'Success: ...' or an
                       exception string. Never NULL.

TABLE: Edge  -- parent/child call relationships within one TaskID
  TraceID         TEXT -- matches Event.TaskID
  FatherNID       TEXT -- matches Event.TID of the parent event
  FatherStartTime INTEGER -- matches Event.StartTime of that SAME parent
                       event -- (FatherNID, FatherStartTime) TOGETHER pick out
                       the one specific parent event (FatherNID alone is
                       ambiguous, since TID repeats across events)
  ChildNID        TEXT -- matches Event.TID of the child event (may still be
                       ambiguous if that TID recurs; Edge does not carry a
                       ChildStartTime to disambiguate further)
  To reconstruct a call: JOIN Edge e ON e.TraceID = Event.TaskID AND
  e.FatherNID = Event.TID AND e.FatherStartTime = Event.StartTime.
  NOTE: not every TaskID has Edge rows even when it has many Events -- this
  is a real gap in the source data, not a query error. Absence of Edge rows
  does not mean absence of activity; check Event directly too.

TABLE: Trace  -- one row of metadata per TaskID
  TaskID      TEXT
  Title       TEXT -- the top-level command, e.g. 'fs -copyToLocal'
  NumReports  INTEGER -- UNRELIABLE, may not match actual Event row count
  NumEdges    INTEGER -- UNRELIABLE, may not match actual Edge row count --
                       do not trust either count column; COUNT(*) the real
                       Event/Edge rows instead if you need a total
  FirstSeen   timestamp -- real wall-clock date this trace was recorded
  LastUpdated timestamp
  StartTime   INTEGER -- same per-host-relative clock as Event.StartTime;
                       comparable only in the same way Event's is
  EndTime     INTEGER

TABLE: Operation  -- one row per HDFS operation type, aggregated across all
                     visible traces (a latency baseline reference, not
                     per-event data)
  OpName       TEXT -- e.g. 'readBlock', 'chooseDataNode'
  Num          INTEGER -- how many observations this baseline is built from
  MaxDelay     INTEGER -- max observed duration, nanoseconds
  MinDelay     INTEGER -- min observed duration, nanoseconds
  AverageDelay REAL    -- mean observed duration, nanoseconds -- compare an
                       Event's (EndTime - StartTime) against this to judge
                       whether it was unusually slow

Rules:
- Always scope a query to ONE TaskID (or a small explicit set) unless the
  question is explicitly about aggregate/baseline behavior via Operation.
- Never reference a table or column not listed above.
- Do not claim two different HostName values' events were simultaneous based
  on StartTime alone -- that comparison is not valid across hosts.
"""
