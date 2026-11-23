"""The pipeline's stages. Each stage finds its own work, so a re-run with nothing new is a no-op: spine and chunk
process the records whose content changed since they last read them (`source_items.spined_hash`, `chunked_hash`)."""


def stages(extract: int = 0) -> dict:
    """Every pipeline stage by name, in run order (shared by `igneous pipeline` and the UI's Rebuild button)."""
    from igneous.graph import (
        backbone,
        entities,
        organizations,
        profile,
        resolve,
        self_identity,
        spine,
        threads,
        topics,
        verify,
    )
    from igneous.graph import extract as ex
    from igneous.rag import chunking, embed

    return {"spine": spine.run, "chunk": chunking.run, "embed": embed.run,
            "extract": lambda s: ex.run(s, limit=extract) if extract else 0,
            "topics": topics.run,
            "entities": lambda s: entities.run(s, log=None),
            # Life threads (trips, classes, jobs, hobbies): after entities, so named projects exist first.
            # Your resume first: where you worked/studied and your projects anchor everything else.
            "profile": lambda s: profile.run(s, log=None),
            "threads": lambda s: threads.run(s, log=None),
            # Threads under the orgs and schools on your resume (classes under your university, projects under the lab).
            "backbone": lambda s: backbone.run(s, log=None),
            # self before resolve: the "you" links must exist when identities are merged
            "self": lambda s: self_identity.run(s)["self_links"],
            "resolve": lambda s: resolve.run(s)["same_as"],
            # Resolve first so aliases (including phone-only contacts) receive one relationship.
            "organizations": organizations.run,
            # Last: check every derived relationship against its evidence (keeps, drops, or flags it).
            "verify": lambda s: verify.run(s, log=None)}
