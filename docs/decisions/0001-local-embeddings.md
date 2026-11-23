# 0001 — Embeddings: a local model, `bge-small-en-v1.5` (384-d)

**Status:** decided 2026-10-19, with 0002. **Affects:** dense search, Ask's evidence, topic merges, criterion 5
(issue #10).

## What had to be decided

Every chunk gets a vector: dense search ranks by it (fused with lexical search), the topics stage merges
near-duplicate topics by it, and commitment checks rank later messages by it. The column's dimension is part of the
schema (`chunks.embedding vector(384)`, migration 0001), so the model had to be chosen before data was embedded at
scale.

The proposal assumed Bedrock Knowledge Bases would read our vectors as they are, which would have tied us to
Bedrock's embedding model (Titan Text Embeddings v2, 1024-d). Decision 0002 found that Bedrock re-embeds whatever it
ingests and was dropped from criterion 5, so no consuming engine constrains the model or its dimension. LightRAG
takes any embedding function.

## Options

| | Model | Where it runs | What leaves the computer | Notes |
|---|---|---|---|---|
| A | Titan Text Embeddings v2 (1024-d) | Bedrock | the text of every record, to be embedded | priced per token; needs credentials before anything is searchable |
| B | Cohere Embed v3 (1024-d) | Bedrock | the text of every record | as A |
| C | `bge-m3` (1024-d, multilingual) | this computer | nothing | much slower on a laptop CPU |
| D | `bge-small-en-v1.5` (384-d) | this computer (FastEmbed, ONNX) | nothing | fast on a laptop CPU; English |

## Decision

**D.** Embeddings are made on this computer by `BAAI/bge-small-en-v1.5` through FastEmbed, for every source.

- **Privacy:** indexing never sends a record anywhere. The opt-in for hosted models (`IGNEOUS_LLM_SOURCES`) governs
  only what a hosted model reads to answer or extract; a hosted embedding model would have needed every record's
  text, opted in or not.
- **Cost and availability:** search works with no credentials and no per-token cost, so a fresh install is useful
  before any model is configured.
- **Speed:** it embeds the six-mailbox Enron baseline (about 18,500 chunks) on a laptop CPU in under an hour, and new
  records as they arrive.
- **Interop:** LightRAG answers from our exported graph with the same embedding function, and the MCP server reads
  the graph directly, so criterion 5 holds without a shared vector space.

Answers are reranked by a local cross-encoder (`Xenova/ms-marco-MiniLM-L-6-v2`), also on this computer.

## Consequences

- `chunks.embedding_model` names the model behind each vector (migration 0011). The embed stage re-embeds a vector
  from any other model, and dense search compares only vectors from the configured one, so changing
  `IGNEOUS_EMBEDDING_MODEL` never mixes two vector spaces.
- Another 384-dimensional model is a settings change plus one re-embed. Another dimension (C, or A and B) also needs a
  migration of the column.
- The model is English-only. Non-English records are still found by lexical search, and C is the upgrade path if
  they matter.
