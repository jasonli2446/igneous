"""One organization, one key: a domain that belongs to a company is that company's node.

An organization found by its email domain was keyed by the domain (`acme.com`) even when the company it belongs to had
a node of its own (`org:acme-robotics`, from LinkedIn, a contact card or your resume), so one organization was two
nodes, and which one your resume's link went to changed between builds. The organizations stage now keys such a
domain as the company (graph/organizations.py) and lists it in the company's `props.domains`: the company your resume
names at that domain (keyed by the name the domain's own people give it, when they give one), the company its people
name, or the company the model names, when another record names that company too.

The next build moves every derived link to the company. What this migration moves is what that build can't: each
domain organization the company now holds takes the company's key (or, where the company's node exists, gives it its
links and goes), its backbone thread follows (`backbone:<domain>` -> `backbone:<company>`), and your corrections and
the verifier's verdicts, which are keyed by natural keys, follow them, so a link you rejected stays rejected and a name
you gave the org's thread stays.

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision = "0016"
down_revision = "0015"

# A company name's key, as organizations.company_key makes it: org:<slug>.
SLUG = "trim(both '-' from regexp_replace(lower({0}), '[^a-z0-9]+', '-', 'g'))"


def upgrade():
    op.execute(f"""
        -- The company each domain's own people name (their contact cards and LinkedIn connections), as
        -- organizations._own_words counts it: once per record, the most named first, then the longest name.
        CREATE TEMP TABLE company_words ON COMMIT DROP AS
        SELECT DISTINCT ON (d) d, company FROM (
            SELECT d, company, count(DISTINCT sid) AS n FROM (
                SELECT si.id AS sid, lower(split_part(a.addr, '@', 2)) AS d,
                       regexp_replace(btrim(c.company), '\\s+', ' ', 'g') AS company
                FROM source_items si
                CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(si.raw_meta->'emails', '[]')) a(addr)
                CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(si.raw_meta->'orgs', '[]')) c(company)
                WHERE si.source IN ('gcontacts', 'maccontacts')
                UNION ALL
                SELECT si.id, lower(split_part(si.raw_meta->>'email', '@', 2)),
                       regexp_replace(btrim(si.raw_meta->>'company'), '\\s+', ' ', 'g')
                FROM source_items si WHERE si.source = 'linkedin' AND si.raw_meta->>'kind' = 'connection'
            ) x WHERE d <> '' AND COALESCE(company, '') <> '' GROUP BY d, company
        ) w ORDER BY d, n DESC, length(company) DESC, company COLLATE "C" DESC;

        -- Each domain organization a company now holds, with the company's key.
        CREATE TEMP TABLE company_rekey ON COMMIT DROP AS
        SELECT n.id AS old, n.key AS old_key, 'org:' || k.slug AS key, NULL::bigint AS survivor
        FROM nodes n LEFT JOIN company_words w ON w.d = n.key
        CROSS JOIN LATERAL (SELECT CASE WHEN n.props->>'named_by' = 'profile' AND w.company IS NOT NULL
                                        THEN {SLUG.format('w.company')} ELSE {SLUG.format('n.name')} END AS slug) k
        WHERE n.type = 'Organization' AND n.key !~ ':' AND n.key ~ '\\.' AND length(k.slug) BETWEEN 2 AND 100
          AND (n.props->>'named_by' = 'profile' OR n.props->>'named_from' = 'their own words'
               OR (n.props->>'named_from' = 'how its people sign'
                   AND EXISTS (SELECT 1 FROM nodes c WHERE c.type = 'Organization' AND c.key = 'org:' || k.slug)));
        UPDATE company_rekey r SET survivor = COALESCE(
            (SELECT x.id FROM nodes x WHERE x.type = 'Organization' AND x.key = r.key),
            (SELECT min(o.old) FROM company_rekey o WHERE o.key = r.key));

        -- Their backbone threads follow them.
        CREATE TEMP TABLE company_thread_rekey ON COMMIT DROP AS
        SELECT DISTINCT ON (t.id) t.id AS old, t.key AS old_key, 'backbone:' || r.key AS key, NULL::bigint AS survivor
        FROM company_rekey r JOIN nodes t ON t.type = 'Project' AND t.key = 'backbone:' || r.old_key ORDER BY t.id;
        UPDATE company_thread_rekey r SET survivor = COALESCE(
            (SELECT x.id FROM nodes x WHERE x.type = 'Project' AND x.key = r.key),
            (SELECT min(o.old) FROM company_thread_rekey o WHERE o.key = r.key));

        -- Natural keys, as corrections and verdicts store them.
        CREATE TEMP TABLE company_natural_rekey ON COMMIT DROP AS
        SELECT DISTINCT 'Organization:' || old_key AS old_key, 'Organization:' || key AS key FROM company_rekey
        UNION SELECT DISTINCT 'Project:' || old_key, 'Project:' || key FROM company_thread_rekey;
    """)
    op.execute("""
        DO $$
        DECLARE rk record;
        BEGIN
            -- A node whose key another node has: its links move there (unless that node has the same link), then it
            -- goes; otherwise it takes the key.
            FOR rk IN SELECT old, survivor FROM company_rekey WHERE old <> survivor
                     UNION ALL SELECT old, survivor FROM company_thread_rekey WHERE old <> survivor ORDER BY 1 LOOP
                UPDATE edges e SET dst = rk.survivor WHERE e.dst = rk.old AND e.src <> rk.survivor
                  AND NOT EXISTS (SELECT 1 FROM edges x WHERE x.src = e.src AND x.dst = rk.survivor
                                    AND x.type = e.type AND x.extractor = e.extractor);
                UPDATE edges e SET src = rk.survivor WHERE e.src = rk.old AND e.dst <> rk.survivor
                  AND NOT EXISTS (SELECT 1 FROM edges x WHERE x.src = rk.survivor AND x.dst = e.dst
                                    AND x.type = e.type AND x.extractor = e.extractor);
                UPDATE nodes SET props = props || jsonb_build_object('thread', rk.survivor)
                    WHERE type = 'Organization' AND props->>'thread' = rk.old::text;
                UPDATE nodes SET props = props || jsonb_build_object('org', rk.survivor)
                    WHERE type = 'Project' AND props->>'org' = rk.old::text;
                UPDATE edges_dropped SET src = rk.survivor WHERE src = rk.old;
                UPDATE edges_dropped SET dst = rk.survivor WHERE dst = rk.old;
                DELETE FROM nodes WHERE id = rk.old;
            END LOOP;
            UPDATE nodes n SET key = r.key FROM company_rekey r WHERE n.id = r.old AND r.old = r.survivor;
            UPDATE nodes n SET key = r.key FROM company_thread_rekey r WHERE n.id = r.old AND r.old = r.survivor;
            UPDATE nodes n SET props = n.props || jsonb_build_object('domains', d.domains)
                FROM (SELECT survivor, jsonb_agg(old_key ORDER BY old_key) AS domains FROM company_rekey GROUP BY survivor) d
                WHERE n.id = d.survivor;
            -- Each company stands for the backbone thread its domain organization had, if it had none of its own.
            UPDATE nodes o SET props = o.props || jsonb_build_object('thread', t.id)
                FROM (SELECT DISTINCT survivor, key FROM company_rekey) r JOIN nodes t ON t.type = 'Project'
                     AND t.key = 'backbone:' || r.key
                WHERE o.id = r.survivor AND NOT o.props ? 'thread';
            UPDATE nodes t SET props = t.props || jsonb_build_object('org', r.survivor)
                FROM (SELECT DISTINCT survivor, key FROM company_rekey) r
                WHERE t.type = 'Project' AND t.key = 'backbone:' || r.key;

            -- Your corrections and the verifier's verdicts follow the keys. A verdict already on the new pair stays,
            -- unless only the moved one is yours.
            UPDATE overrides o SET node_keys = ARRAY(
                    SELECT COALESCE(m.key, k) FROM unnest(o.node_keys) WITH ORDINALITY u(k, i)
                    LEFT JOIN company_natural_rekey m ON m.old_key = u.k ORDER BY u.i)
                WHERE o.node_keys && ARRAY(SELECT old_key FROM company_natural_rekey);
            FOR rk IN SELECT old_key, key FROM company_natural_rekey ORDER BY 1 LOOP
                UPDATE overrides SET data = CAST(replace(CAST(data AS text), to_json(rk.old_key)::text,
                                                         to_json(rk.key)::text) AS jsonb)
                    WHERE strpos(CAST(data AS text), to_json(rk.old_key)::text) > 0;
            END LOOP;
            UPDATE thread_parents p SET child_key = COALESCE(
                    (SELECT substr(m.key, 9) FROM company_natural_rekey m WHERE m.old_key = 'Project:' || p.child_key), child_key),
                parent_key = COALESCE(
                    (SELECT substr(m.key, 9) FROM company_natural_rekey m WHERE m.old_key = 'Project:' || p.parent_key), parent_key)
                WHERE 'Project:' || child_key IN (SELECT old_key FROM company_natural_rekey)
                   OR 'Project:' || parent_key IN (SELECT old_key FROM company_natural_rekey);
            INSERT INTO edge_verdicts (src_key, dst_key, type, extractor, evidence, verdict, confidence, reason, source,
                                       updated_at)
                SELECT DISTINCT ON (1, 2, 3, 4) COALESCE(a.key, v.src_key), COALESCE(b.key, v.dst_key), v.type,
                       v.extractor, v.evidence, v.verdict, v.confidence, v.reason, v.source, v.updated_at
                FROM edge_verdicts v LEFT JOIN company_natural_rekey a ON a.old_key = v.src_key
                LEFT JOIN company_natural_rekey b ON b.old_key = v.dst_key
                WHERE a.key IS NOT NULL OR b.key IS NOT NULL
                ORDER BY 1, 2, 3, 4, v.source = 'user' DESC, v.updated_at DESC
            ON CONFLICT (src_key, dst_key, type, extractor) DO UPDATE SET evidence = EXCLUDED.evidence,
                verdict = EXCLUDED.verdict, confidence = EXCLUDED.confidence, reason = EXCLUDED.reason,
                source = EXCLUDED.source, updated_at = EXCLUDED.updated_at
                WHERE edge_verdicts.source <> 'user' AND EXCLUDED.source = 'user';
            -- A dropped link follows its verdict, once per new pair; the rest go with the old verdicts.
            WITH moved AS (
                SELECT d.src_key AS os, d.dst_key AS od, d.type, d.extractor, COALESCE(a.key, d.src_key) AS ns,
                       COALESCE(b.key, d.dst_key) AS nd
                FROM edges_dropped d LEFT JOIN company_natural_rekey a ON a.old_key = d.src_key
                LEFT JOIN company_natural_rekey b ON b.old_key = d.dst_key
                WHERE a.key IS NOT NULL OR b.key IS NOT NULL),
            pick AS (
                SELECT DISTINCT ON (ns, nd, type, extractor) * FROM moved m
                WHERE NOT EXISTS (SELECT 1 FROM edges_dropped x WHERE x.src_key = m.ns AND x.dst_key = m.nd
                                    AND x.type = m.type AND x.extractor = m.extractor)
                ORDER BY ns, nd, type, extractor, os, od)
            UPDATE edges_dropped d SET src_key = p.ns, dst_key = p.nd FROM pick p
                WHERE d.src_key = p.os AND d.dst_key = p.od AND d.type = p.type AND d.extractor = p.extractor;
            DELETE FROM edge_verdicts v WHERE v.src_key IN (SELECT old_key FROM company_natural_rekey)
                                           OR v.dst_key IN (SELECT old_key FROM company_natural_rekey);
        END $$;
        DROP TABLE company_words, company_rekey, company_thread_rekey, company_natural_rekey;
    """)


def downgrade():
    pass  # the company keys stay; code before this keys a domain its own way on the next build and leaves these alone
