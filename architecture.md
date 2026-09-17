# Architecture

`SPEC.md` is the design document; this file is the picture of it.

Start with the two diagrams below -- the pieces, and the request flow. The
sections after them are detail, and only worth opening when you need it.

---

## Components

What exists, and what talks to what. (This is a *component diagram* -- C4
calls the same thing a container diagram.)

```mermaid
flowchart LR
    user(["User"])

    subgraph puma["PUMA"]
        chat["HelpBot"]
    end

    agent["<b>BI Agent</b><br/><i>decides which tools to call,<br/>writes the answer</i>"]

    subgraph r2d2["R2D2 gateway"]
        model["Model"]
    end

    subgraph mcps["MCP servers"]
        direction TB
        sb["<b>SB MCP Tools</b><br/><i>no LLM</i><br/>· Cluster identification and health check<br/>· Access validation<br/>· Config Auditor<br/>· Query Plan Analyzer"]
        rag["RAG / docs"]
    end

    subgraph fleet["Starburst fleet"]
        direction TB
        target["<b>Target cluster</b><br/><i>the one identified</i>"]
        others["other clusters<br/><i>100s</i>"]
    end

    cos[("IBM COS<br/><i>config backups, nightly</i>")]
    audit[("Audit catalog<br/><i>completed queries</i>")]

    user --> chat
    chat --> agent
    agent -->|"prompt / completion"| r2d2
    agent -->|"tool call / findings<br/><i>MCP</i>"| sb
    agent -->|"context<br/><i>MCP</i>"| rag
    sb -->|"reads config backups<br/><i>S3 API, read-only</i>"| cos
    sb -->|"reads query history<br/><i>trino python client, read-only</i>"| audit
    fleet -.->|"backed up nightly"| cos
    target --- audit

    classDef mine fill:#eef7ee,stroke:#4a7,stroke-width:2px
    classDef dim fill:#fafafa,stroke:#bbb,color:#888
    class sb mine
    class others dim
```

**How to read it:** PUMA is where the user types. The BI Agent behind it
reaches the model through the R2D2 gateway, and reaches data through MCP
servers — never the other way round. Cluster identification is part of the SB
MCP Tools already, from the earlier use cases, so identifying the cluster and
auditing it are two calls to the same server rather than a hop between two.

Arrow labels say what crosses the line and how, not which feature sits at the
end of it. The feature names are already inside the box; what a reader cannot
otherwise tell is that one connection is object storage and the other is SQL.

The agent identifies the cluster first, then passes that name into every
following call. The tools never pick a cluster themselves — scope is always
an argument, so there is no `validate_everything()`.

From there the two use cases diverge, and they share nothing but the server
they live in. The Config Auditor reads last night's backup from COS. The
Query Plan Analyzer reads the audit catalog.

> Worth confirming: the diagram shows the audit catalog belonging to the
> identified cluster. If one master cluster federates the whole fleet's audit
> catalogs instead, that arrow moves to the master and the cluster name
> becomes a filter rather than a connection target. The adapter currently
> assumes the federated version — one connection, cluster as a `WHERE`
> clause.

---

## Request flow

The same two paths, in order. (This one is a *sequence diagram*.)

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant P as PUMA
    participant A as BI Agent
    participant R as R2D2 gateway
    participant S as SB MCP Tools
    participant D as COS / Audit catalog

    U->>P: "why was query X slow?"<br/>or "is my cluster set up right?"
    P->>A: forward
    A->>R: which tool fits this?
    R-->>A: call the analyzer

    A->>S: identify cluster
    S-->>A: prod-analytics

    rect rgb(238, 247, 238)
        note over A,D: deterministic - no model inside this band
        A->>S: analyze_query(cluster, query_id)<br/>or run_rules(cluster, scopes)
        S->>D: read-only fetch
        D-->>S: query stats / config files
        note right of S: evaluate rules, run detectors,<br/>attach evidence to every finding
        S-->>A: findings + coverage<br/>(empty list = nothing found)
    end

    A->>R: write this up, findings attached
    R-->>A: plain-English answer
    A->>P: answer, with file+line or query id
    P->>U: shown in chat
```

The green band is the part that cannot vary: same query id, same findings,
same numbers, every time. The model reads those findings and writes the
sentences -- it is not allowed to add a problem the tools did not return.

---

# Detail

Everything below is the inside of the green band. Skip it unless you need it.

The dependency rule every diagram obeys: **arrows point inward.** `mcp_server`
knows about `core`; `core` knows about nothing above it. That is what makes
the eventual LangGraph-to-ADK migration a copy of `core/` plus a new wrapper.

---

## 1. System context

Where the boundaries are, and which side owns the model.

```mermaid
flowchart TB
    user(["Application-team user<br/><i>queries the cluster, does not own it</i>"])

    subgraph agent["PUMA / BI Agent — the model side"]
        direction LR
        llm["BI Agent + R2D2<br/><i>decides which tools to call,<br/>writes the prose</i>"]
        rag["RAG / doc index<br/><i>optional context</i>"]
    end

    subgraph mcp["SB MCP Tools — no LLM"]
        direction LR
        uc1["Use case 1<br/><b>Config Auditor</b><br/>6 tools"]
        uc2["Use case 2<br/><b>Query Plan Analyzer</b><br/>3 tools"]
    end

    cos[("IBM COS<br/>config backups<br/><i>redacted, daily</i>")]
    audit[("Master Starburst cluster<br/>audit catalog<br/><i>completed queries</i>")]

    user -->|"question in English"| llm
    llm -->|"answer with evidence"| user
    rag -.->|"context: non-decisional"| llm
    llm <-->|"MCP: tool call / structured findings"| uc1
    llm <-->|"MCP: tool call / structured findings"| uc2
    uc1 -->|"read-only, S3 API"| cos
    uc2 -->|"read-only SQL, LDAP auth as AD FID"| audit

    classDef nollm fill:#eef7ee,stroke:#4a7,stroke-width:2px
    classDef hasllm fill:#fff4e6,stroke:#e90,stroke-width:2px
    class mcp,uc1,uc2 nollm
    class agent,llm,rag hasllm
```

The green boundary contains no model. Same inputs, same outputs, every time.
Everything that requires judgement — which tool, what to say — is on the
orange side. That split is the whole design: the BI Agent can be replaced
without touching a rule.

---

## 2. Use case 1 — Config auditor

**Question:** *is this cluster configured correctly?*
**Reads:** config backups. **Knows nothing about** any individual query.

```mermaid
flowchart TB
    subgraph tools["MCP tools — thin wrappers, no logic"]
        direction LR
        t1["list_clusters"]
        t2["describe_backup_layout"]
        t3["get_config_summary"]
        t4["run_rules"]
        t5["get_config_detail"]
        t6["diff_clusters"]
    end

    svc["ConfigValidationService<br/><i>core/service.py</i>"]

    subgraph load["Build a snapshot"]
        direction TB
        repo["ConfigRepository (port)<br/><i>COS · local backup</i>"]
        parse["Parsers<br/>.properties · jvm.config · *-site.xml<br/>+ .json sibling cross-check"]
        meta["Metadata<br/><i>owner, group, mode<br/>from *.metadata.json</i>"]
        redact["Redaction guard<br/><i>refuse to surface a secret</i>"]
        snap["ClusterSnapshot<br/><i>every node, every file, with line numbers</i>"]
        repo --> parse --> snap
        repo --> meta --> snap
        parse --> redact --> snap
    end

    subgraph eval["Evaluate — deterministic, no context reachable"]
        direction TB
        cat[("Rule catalog<br/><i>YAML, one file per domain</i>")]
        gate["Version gate<br/><i>true / false / unknown</i>"]
        prop["Property checks<br/>max · min · range · equals<br/>one_of · matches · ratio"]
        cons["Consistency checks<br/><i>same value on every node?</i>"]
        filec["File checks<br/><i>world-readable? mode?</i>"]
        cat --> gate --> prop & cons & filec
    end

    findings["Findings<br/><i>Scope · Severity · Owner<br/>actual vs expected<br/>evidence: file + line</i>"]
    cov["Coverage<br/><i>what could not be checked, and why</i>"]
    enrich["Enrichment pass<br/><i>context applied here and only here</i>"]
    out(["ConfigSummary / RuleResult"])

    tools --> svc --> load
    snap --> eval
    eval --> findings
    eval --> cov
    findings --> enrich --> out
    cov --> out
    ctx[/"context<br/>(optional, from parent)"/] -.->|"may annotate<br/><b>never decides</b>"| enrich

    classDef danger fill:#fff0f0,stroke:#c44,stroke-dasharray:4 3
    class ctx danger
```

Two things the diagram is making a point of:

- **Context enters after the findings exist.** It is not an input to
  evaluation — it cannot be reached from there. That is structural, not a
  convention, which is why a rule cannot be suppressed by a document.
- **Coverage leaves by its own arrow.** An empty findings list means
  "healthy" only when `coverage.complete` is true. Silence because nothing is
  wrong and silence because little was examined look identical otherwise.

---

## 3. Use case 2 — Query analyzer

**Question:** *why was this query slow?*
**Reads:** completed-query history. **Knows nothing about** whether the
cluster is correctly configured.

```mermaid
flowchart TB
    subgraph tools2["MCP tools"]
        direction LR
        q1["analyze_query"]
        q2["compare_queries"]
        q3["get_query_info"]
    end

    svc2["QueryAnalysisService<br/><i>core/analysis/service.py</i>"]
    repo2["QueryRepository (port)<br/><i>Trino audit catalog</i>"]
    prof[("Column profile<br/><i>YAML: audit schema → our field names</i>")]
    qi["QueryInfo<br/><i>timings · volumes · state<br/>· SQL text · operator stats</i>"]

    subgraph sig1["Signal A — what the runtime measured"]
        direction TB
        dt["Detectors"]
        dtime["timing<br/><i>queueing dominates</i>"]
        dvol["volume<br/><i>scan amplification, spill</i>"]
        dop["operators<br/><i>join explosion, broadcast,<br/>skew, selectivity</i>"]
        dt --> dtime & dvol & dop
    end

    subgraph sig2["Signal B — what the SQL says"]
        direction TB
        sp["sqlglot parse"]
        pat["Patterns<br/><i>function on partition column,<br/>cross join, SELECT *</i>"]
        rw["AST rewrite<br/><i>marked equivalent or suggested</i>"]
        sp --> pat --> rw
    end

    corr{{"Correlate<br/><i>both signals agree?</i>"}}
    root["is_root_cause = true<br/><i>state it firmly</i>"]
    susp["suspected<br/><i>raise it as a question</i>"]

    hist["History — automatic<br/><i>same SQL, previous run</i>"]
    out2(["QueryAnalysis<br/>findings · suggested_sql<br/>history · coverage"])

    tools2 --> svc2 --> repo2
    prof -.->|"maps columns"| repo2
    repo2 --> qi
    qi --> sig1
    qi -->|"SQL text"| sig2
    sig1 --> corr
    sig2 --> corr
    corr -->|"yes"| root
    corr -->|"one signal only"| susp
    svc2 -.->|"best-effort,<br/>never blocks"| hist
    root & susp & hist --> out2
```

The correlation node is the interesting part. A huge scan on its own is a
symptom; `year(order_date)` in a WHERE clause on its own is a suspicion.
The two together are a root cause, and only then does the answer stop
hedging.

`history` hangs off the side deliberately: a query with no earlier run is
normal, and a failed lookup costs the context, never the findings.

---

## 4. Why the two stay separate

They share the *shape* of a finding and nothing else. mypy proves the
taxonomies cannot overlap, and a test asserts it.

```mermaid
flowchart LR
    subgraph shared["Shared shape — core/models.py"]
        f["Finding<br/>severity · owner · evidence<br/>summary · rationale · next_step"]
    end

    subgraph c1["Config finding"]
        s["domain: <b>Scope</b><br/><i>memory · jvm · node_identity<br/>file_security · catalog …</i>"]
        e1["evidence: ConfigEvidence<br/><i>role · node · file · line</i>"]
    end

    subgraph c2["Query finding"]
        d["domain: <b>QueryDomain</b><br/><i>scheduling · data_access<br/>memory · join · …</i>"]
        e2["evidence: QueryEvidence<br/><i>stage · operator · metric</i>"]
    end

    f --> c1
    f --> c2
    c1 x--x|"cannot mix"| c2
```

Tagging a slow query with a config `Scope` would claim the slowness is a fact
about `catalog/*.properties`. It is not, and a user who acts on that claim
goes looking in the wrong file.

---

## 5. Deployment

```mermaid
flowchart TB
    subgraph ocp["OpenShift"]
        direction TB
        pod["Pod — random UID<br/>ubi9/python-311<br/>HOME=/tmp"]
        sec[/"Secrets<br/><i>mounted files or env</i><br/>COS keys · TRINO_PASSWORD_FILE"/]
        sec -->|"read at startup,<br/>never logged"| pod
    end

    parent["BI Agent"] <-->|"MCP"| pod
    pod -->|"HTTPS"| cos[("IBM COS")]
    pod -->|"TLS + LDAP as AD FID"| sb[("Master Starburst<br/>audit catalog")]

    note["All 9 tools are read-only.<br/>Anything that would load or mutate<br/>a cluster splits into plan_* / run_*<br/>so the parent can gate it."]
    pod -.- note

    classDef n fill:#f8f8f8,stroke:#999,stroke-dasharray:3 3
    class note n
```

---

## 6. What the layers may import

The rule that survives every migration.

```mermaid
flowchart TD
    m["<b>mcp_server/</b><br/>thin MCP wrapper<br/><i>no business logic</i>"]
    a["<b>adapters/</b><br/>COS · Trino · local backup<br/><i>network lives here</i>"]
    c["<b>core/</b><br/>rules · analysis · parsers<br/><i>pure, offline, fully unit-tested</i>"]
    p["<b>core/ports.py</b><br/>Protocols<br/><i>ConfigRepository · QueryRepository</i>"]

    m -->|"depends on"| c
    m -->|"wires up"| a
    a -.->|"implements"| p
    p --- c

    x["agent framework<br/>LLM client<br/>MCP library"]
    c -.->|"<b>never</b>"| x

    classDef forbidden fill:#fff0f0,stroke:#c44,stroke-width:2px
    class x forbidden
```

`core/` importing an agent framework is the one thing that would make the
migration a rewrite instead of a copy. Nothing there imports over the
network at module load either, which is why the whole suite runs offline in
about two seconds.
