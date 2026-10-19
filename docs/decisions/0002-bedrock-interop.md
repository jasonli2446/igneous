# 0002 — Bedrock Knowledge Bases interop: the proposal's plan doesn't match how Bedrock works

**Status:** open, needs a team decision. **Affects:** success criterion 5, issues #10 and #56.

## What the proposal said

> Bedrock Knowledge Bases can be configured against a customer-managed Aurora PostgreSQL vector store …
> we satisfy this with a database **view** over our chunk table … with no re-embedding.

## What AWS actually requires

Per the [Bedrock KB vector-store prerequisites](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup-rds.html):

- The store is a **table** in Aurora (same AWS account) with a **UUID primary key**, a vector column, a text
  column, a Bedrock-managed `metadata` JSON column, and optional `custom_metadata` JSONB.
- An HNSW index on the vector column and a GIN index on the text column are **required** — you can't
  index a view.
- **Bedrock owns ingestion.** A data source sync reads documents, chunks them, embeds them with the KB's
  embedding model, and writes rows itself. The metadata column holds Bedrock's own source-attribution data.
- Supported embedding dimensions are model-fixed (Titan v2: 1024/512/256; Cohere: 1024). Ours are 384.

So "point Bedrock at our existing vectors" isn't a supported path. Pre-filling the table with our rows may
work for retrieval, since Bedrock just queries it, but nothing documents it and a data-source sync could
overwrite or delete those rows.

## Options

| | What | Re-embeds? | Keeps our graph? | Cost |
|---|---|---|---|---|
| A | Push our chunks into a KB through `IngestKnowledgeBaseDocuments` (custom data source), graph context (participants, thread, resolved people, topics) as metadata attributes | **yes**, Bedrock embeds | as metadata, filterable | Aurora or S3 Vectors + Titan calls |
| B | Pre-fill a Bedrock-shaped table with Titan-v2 vectors we compute, never sync | no (if we switch to Titan, #10) | as metadata | Aurora + an unsupported setup to test |
| C | Drop Bedrock from criterion 5; LightRAG is the shipped target (done, verified) and the MCP server (#58) is the second | — | yes, natively | none |

**Recommendation: C, with A as a stretch.** LightRAG already shows the creation-layer claim at full strength:
it loads our entities and relationships with zero re-extraction and answers from them
(`api/eval/lightrag_demo.py`). Bedrock can't consume a graph, only chunks plus metadata, so even a
working A or B shows less. If the team wants Bedrock anyway, A is the supported route; reword criterion 5 to
"no re-*extraction*" rather than "no re-embedding".

Any option touching AWS needs someone to provision and pay for Aurora or S3 Vectors. Not done here.
