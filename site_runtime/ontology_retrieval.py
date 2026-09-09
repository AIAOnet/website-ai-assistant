"""Bounded source discovery, not logical inference or answer evidence."""
import re
import unicodedata
from collections import deque
from types import SimpleNamespace

from .ontology_admin import inspect_ontology

MAX_SEEDS = 8
MAX_VISITED = 60
MAX_PATHS = 32
MAX_BRANCHES = 8
DEPTH_SCORES = {1: .6, 2: .4}


def normalize(value):
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFC", value).casefold()))


def search_ontology(ontology, knowledge, contacts, query, language, category=None, depth=1):
    result = {"tool": "search_ontology", "matched_entities": [], "matched_aliases": [],
              "expanded_entities": [], "source_ids": [], "source_scores": {},
              "relationship_rows": [], "paths": [], "fallback_reason": None,
              "max_hops": depth, "truncated": False,
              "limits": {"seeds": MAX_SEEDS, "visited_entities": MAX_VISITED,
                         "paths": MAX_PATHS, "branches_per_entity": MAX_BRANCHES}}
    if type(depth) is not int or depth not in {1, 2}:
        result.update(max_hops=None, fallback_reason="invalid_depth")
        return result
    if (not isinstance(ontology, dict) or
            not isinstance(ontology.get("relationships"), list) or
            len(ontology["relationships"]) > 4000):
        result["fallback_reason"] = "invalid_ontology"
        return result
    report = inspect_ontology(SimpleNamespace(ontology=ontology, knowledge=knowledge, contacts=contacts))
    if (report["issues"] or any(row["issues"] for row in report["relationships"]) or
            any(row["issues"] for row in report["aliases"])):
        result["fallback_reason"] = "invalid_ontology"
        return result

    text = " " + normalize(query[:500]) + " "
    sources = {r["source_id"]: r for r in knowledge.records}
    url_scores, seeds = {}, set()
    for alias in report["aliases"]:
        if alias["language"] == language and " " + normalize(alias["alias"]) + " " in text:
            seeds.add(alias["entity"])
            result["matched_aliases"].append({"alias": alias["alias"], "entity": alias["entity"],
                                              "language": alias["language"], "row": alias["row"]})
            url = sources[alias["source_id"]]["canonical_url"]
            url_scores[url] = max(url_scores.get(url, 0), DEPTH_SCORES[1])

    adjacency = {}
    for row in report["relationships"]:
        # Contact edges never participate in LLM retrieval or traversal.
        if row["predicate"] == "RESPONSIBLE_CONTACT":
            continue
        labels = [row["subject"]]
        if row["predicate"] != "DOCUMENTED_BY":
            labels.append(row["object"])
        for label in labels:
            if normalize(label) and " " + normalize(label) + " " in text:
                seeds.add(label)
        adjacency.setdefault(row["subject"], []).append((row["object"], row))
        adjacency.setdefault(row["object"], []).append((row["subject"], row))
    ordered_seeds = sorted(seeds)
    if len(ordered_seeds) > MAX_SEEDS:
        ordered_seeds = ordered_seeds[:MAX_SEEDS]
        result["truncated"] = True
    result["matched_entities"] = ordered_seeds

    visited_entities, path_signatures, relationship_rows = set(ordered_seeds), set(), set()
    for seed in ordered_seeds:
        queue = deque([(seed, [seed], [], [], [])])
        while queue and len(result["paths"]) < MAX_PATHS and len(visited_entities) <= MAX_VISITED:
            current, entities, rows, predicates, path_source_ids = queue.popleft()
            current_depth = len(rows)
            if current_depth >= depth:
                continue
            branches = sorted(adjacency.get(current, []), key=lambda value: (value[1]["row"], value[0]))
            if len(branches) > MAX_BRANCHES:
                branches = branches[:MAX_BRANCHES]
                result["truncated"] = True
            for neighbor, row in branches:
                if len(result["paths"]) >= MAX_PATHS:
                    result["truncated"] = True
                    break
                if neighbor in entities:  # Per-path cycle prevention.
                    continue
                if neighbor not in visited_entities and len(visited_entities) >= MAX_VISITED:
                    result["truncated"] = True
                    continue
                next_depth = current_depth + 1
                next_entities = entities + [neighbor]
                next_rows = rows + [row["row"]]
                next_predicates = predicates + [row["predicate"]]
                next_source_ids = path_source_ids + [row["source_id"]]
                if row["predicate"] == "DOCUMENTED_BY":
                    next_source_ids.append(row["object"])
                next_source_ids = list(dict.fromkeys(next_source_ids))
                signature = tuple(next_rows)
                if signature not in path_signatures:
                    path_signatures.add(signature)
                    result["paths"].append({"depth": next_depth, "entities": next_entities,
                                            "predicates": next_predicates,
                                            "relationship_rows": next_rows,
                                            "source_ids": next_source_ids})
                relationship_rows.add(row["row"])
                score = DEPTH_SCORES[next_depth]
                url = sources[row["source_id"]]["canonical_url"]
                url_scores[url] = max(url_scores.get(url, 0), score)
                if row["predicate"] == "DOCUMENTED_BY":
                    documented = sources[row["object"]]["canonical_url"]
                    url_scores[documented] = max(url_scores.get(documented, 0), score)
                if neighbor not in visited_entities:
                    visited_entities.add(neighbor)
                    if next_depth < depth:
                        queue.append((neighbor, next_entities, next_rows, next_predicates,
                                      next_source_ids))
        if queue or len(result["paths"]) >= MAX_PATHS:
            result["truncated"] = True
    result["expanded_entities"] = sorted(visited_entities - set(ordered_seeds))
    result["relationship_rows"] = sorted(relationship_rows)

    eligible = [r for r in knowledge.records if r["canonical_url"] in url_scores
                and r["language"] == language and (not category or r["category"] == category)]
    result["source_scores"] = {r["source_id"]: url_scores[r["canonical_url"]] for r in eligible}
    result["source_ids"] = sorted(result["source_scores"])
    return result
