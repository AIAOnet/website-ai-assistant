"""Conservative, source-bound ontology from headings and explicit statements."""
import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .knowledge import INJECTION_PATTERNS, validate_record

MAX_ENTITIES = 2000
MAX_RELATIONSHIPS = 4000
MAX_HEADINGS_PER_SOURCE = 40
EntityType = Literal["topic", "organization", "product", "service", "category", "location", "business_contact"]
PREFIXES = {"company":"organization", "organization":"organization", "företag":"organization",
            "organisation":"organization", "product":"product", "produkt":"product",
            "service":"service", "tjänst":"service", "category":"category", "kategori":"category",
            "location":"location", "plats":"location", "contact":"business_contact", "kontakt":"business_contact"}
CATEGORIES = {"products", "produkter", "services", "tjänster", "locations", "platser", "categories", "kategorier"}
VERBS = {"PROVIDES": {"en":"provides", "sv":"erbjuder"},
         "BELONGS_TO": {"en":"belongs to", "sv":"tillhör"},
         "HAS_LOCATION": {"en":"is located in", "sv":"ligger i"}}
COMPATIBLE = {"PROVIDES": ({"organization"},{"product","service"}),
              "BELONGS_TO": ({"product","service"},{"category"}),
              "HAS_LOCATION": ({"organization"},{"location"})}


def key(prefix, *parts):
    return prefix + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]


def heading_entity(heading):
    """Unlabeled headings are topics, never guessed products or organizations."""
    if not 1 <= len(heading) <= 160 or INJECTION_PATTERNS.search(heading) or any(c in heading for c in "<>\n\r"):
        return None
    prefix, separator, label = heading.partition(":")
    kind = PREFIXES.get(prefix.strip().casefold()) if separator else None
    label = label.strip() if kind else heading
    kind = kind or ("category" if heading.casefold() in CATEGORIES else "topic")
    if not label:
        return None
    abbreviation = re.fullmatch(r"(.+?)\s+\(([A-ZÅÄÖ][A-ZÅÄÖ0-9-]{1,11})\)",label)
    aliases = []
    if abbreviation:
        label = abbreviation.group(1).strip()
        aliases = [abbreviation.group(2)]
    return kind, label, aliases


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(StrictModel):
    evidence_id: str
    source_id: str
    source_url: str
    source_checksum: str
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1,max_length=500)
    kind: Literal["heading","statement"]


class Entity(StrictModel):
    entity_id: str
    type: EntityType
    label: str = Field(min_length=1,max_length=160)
    language: Literal["en","sv"]
    source_id: str
    heading_index: int = Field(ge=0)
    evidence_ids: list[str] = Field(min_length=1,max_length=1)
    extraction_method: Literal["explicit_heading"] = "explicit_heading"
    review_status: Literal["automated"] = "automated"


class Relationship(StrictModel):
    relationship_id: str
    subject_id: str
    predicate: Literal["DOCUMENTED_BY","PROVIDES","BELONGS_TO","HAS_LOCATION"]
    object_id: str
    evidence_ids: list[str] = Field(min_length=1,max_length=1)
    extraction_method: Literal["explicit_heading","explicit_statement"]
    review_status: Literal["automated"] = "automated"


class Alias(StrictModel):
    alias_id: str
    entity_id: str
    label: str = Field(min_length=1,max_length=160)
    language: Literal["en","sv"]
    evidence_ids: list[str] = Field(min_length=1,max_length=1)
    review_status: Literal["automated"] = "automated"


class Ontology(StrictModel):
    version: Literal[1] = 1
    builder_version: Literal[1] = 1
    generated_at: str
    source_checksums: dict[str,str] = Field(max_length=1000)
    entities: list[Entity] = Field(max_length=MAX_ENTITIES)
    relationships: list[Relationship] = Field(max_length=MAX_RELATIONSHIPS)
    aliases: list[Alias] = Field(max_length=MAX_ENTITIES)
    evidence: list[Evidence] = Field(max_length=MAX_RELATIONSHIPS)
    truncated: bool = False
    ignored_headings: int = Field(ge=0,default=0)
    review_status: Literal["automated"] = "automated"


def entity_key(source_id, sections, index, kind, label):
    # Adding an unrelated heading or changing body text does not change identity.
    occurrence = sum(1 for section in sections[:index]
                     if (heading_entity(section.get("label","")) or ())[:2] == (kind,label))
    return key("entity_",source_id,kind,label,str(occurrence))


def statement_supported(quote, predicate, subject, target):
    compatible = COMPATIBLE.get(predicate)
    if (not compatible or subject.type not in compatible[0] or target.type not in compatible[1]
            or subject.language != target.language or subject.source_id != target.source_id):
        return False
    expected = subject.label + " " + VERBS[predicate][subject.language] + " " + target.label
    return quote in {expected, expected+"."}


def validate_ontology(payload, records):
    """Check evidence alignment and explicit extraction rules, not just references."""
    graph = Ontology.model_validate(payload)
    checked = [validate_record(record) for record in records]
    registry = {r["source_id"]:r for r in checked if r["source_status"] == "active"}
    if len(registry) != len(checked) or graph.source_checksums != {identifier:r["checksum"] for identifier,r in registry.items()}:
        raise ValueError("Ontology source snapshot differs")
    evidence = {item.evidence_id:item for item in graph.evidence}
    entities = {item.entity_id:item for item in graph.entities}
    if (len(evidence) != len(graph.evidence) or len(entities) != len(graph.entities)
            or len({r.relationship_id for r in graph.relationships}) != len(graph.relationships)
            or len({a.alias_id for a in graph.aliases}) != len(graph.aliases)):
        raise ValueError("Duplicate ontology identifiers")
    for item in graph.evidence:
        source = registry.get(item.source_id)
        if (not source or source["canonical_url"] != item.source_url or source["checksum"] != item.source_checksum
                or not item.start < item.end <= len(source["content"])
                or source["content"][item.start:item.end] != item.quote
                or item.evidence_id != key("ev_",item.source_id,str(item.start),str(item.end),item.kind)):
            raise ValueError("Ontology evidence is not aligned with the source")
    for item in graph.entities:
        proof = evidence.get(item.evidence_ids[0])
        source = registry.get(item.source_id)
        description = heading_entity(proof.quote) if proof else None
        sections = source.get("sections",[]) if source else []
        if (not proof or proof.kind != "heading" or proof.source_id != item.source_id or not description
                or description[:2] != (item.type,item.label) or source["language"] != item.language
                or item.heading_index >= len(sections)
                or sections[item.heading_index].get("start") != proof.start
                or sections[item.heading_index].get("label") != proof.quote
                or item.entity_id != entity_key(item.source_id,sections,item.heading_index,item.type,item.label)):
            raise ValueError("Entity is not supported by its heading")
    signatures = set()
    for edge in graph.relationships:
        subject = entities.get(edge.subject_id)
        proof = evidence.get(edge.evidence_ids[0])
        if not subject or not proof or subject.source_id != proof.source_id:
            raise ValueError("Relationship has missing or mismatched evidence")
        if edge.predicate == "DOCUMENTED_BY":
            supported = (edge.object_id == subject.source_id and edge.evidence_ids == subject.evidence_ids
                         and edge.extraction_method == "explicit_heading")
        else:
            target = entities.get(edge.object_id)
            source = registry[proof.source_id]
            # Accept complete paragraphs only: no clipping a negated/qualified sentence.
            paragraph_boundary = (proof.start == 0 or source["content"][proof.start-2:proof.start] == "\n\n") and (
                proof.end == len(source["content"]) or source["content"][proof.end:proof.end+2] == "\n\n")
            supported = (target is not None and proof.kind == "statement" and paragraph_boundary
                         and edge.extraction_method == "explicit_statement"
                         and statement_supported(proof.quote,edge.predicate,subject,target)
                         and all(sum(1 for entity in entities.values() if entity.source_id == item.source_id
                                     and entity.language == item.language and entity.label == item.label) == 1
                                 for item in (subject,target)))
        signature = (edge.subject_id,edge.predicate,edge.object_id,edge.evidence_ids[0])
        if not supported or signature in signatures or edge.relationship_id != key("rel_",*signature):
            raise ValueError("Unsupported or duplicate relationship")
        signatures.add(signature)
    for alias in graph.aliases:
        entity = entities.get(alias.entity_id)
        proof = evidence.get(alias.evidence_ids[0])
        description = heading_entity(proof.quote) if proof else None
        if (not entity or not description or alias.label not in description[2]
                or alias.evidence_ids != entity.evidence_ids or alias.language != entity.language
                or alias.alias_id != key("alias_",alias.entity_id,alias.label)):
            raise ValueError("Alias is not explicit in the source heading")
    return graph.model_dump()


def build_ontology(records, *, checkpoint=lambda:None):
    if len(records) > 1000:
        raise ValueError("Ontology source limit exceeded")
    graph = {"version":1,"builder_version":1,"generated_at":datetime.now(timezone.utc).isoformat(),
             "source_checksums":{r["source_id"]:r["checksum"] for r in records},
             "entities":[],"relationships":[],"aliases":[],"evidence":[],"truncated":False,"ignored_headings":0}
    evidence_ids = set()
    def evidence(source,start,end,kind):
        identifier = key("ev_",source["source_id"],str(start),str(end),kind)
        if identifier not in evidence_ids:
            graph["evidence"].append({"evidence_id":identifier,"source_id":source["source_id"],
                "source_url":source["canonical_url"],"source_checksum":source["checksum"],
                "start":start,"end":end,"quote":source["content"][start:end],"kind":kind})
            evidence_ids.add(identifier)
        return identifier
    def relationship(subject,predicate,target,proof,method):
        graph["relationships"].append({"relationship_id":key("rel_",subject,predicate,target,proof),
            "subject_id":subject,"predicate":predicate,"object_id":target,"evidence_ids":[proof],"extraction_method":method})
    for source in sorted(records,key=lambda r:r["source_id"]):
        checkpoint()
        local = []
        sections = source.get("sections",[])
        if len(sections) > MAX_HEADINGS_PER_SOURCE:
            graph["truncated"] = True
        for index,section in enumerate(sections[:MAX_HEADINGS_PER_SOURCE]):
            if len(graph["entities"]) >= MAX_ENTITIES or len(graph["relationships"]) >= MAX_RELATIONSHIPS:
                graph["truncated"] = True
                break
            label, start = section.get("label",""), section.get("start")
            description = heading_entity(label)
            if (not description or type(start) is not int or start < 0
                    or source["content"][start:start+len(label)] != label):
                graph["ignored_headings"] += 1
                continue
            kind, preferred, aliases = description
            identifier = entity_key(source["source_id"],sections,index,kind,preferred)
            proof = evidence(source,start,start+len(label),"heading")
            entity = Entity(entity_id=identifier,type=kind,label=preferred,language=source["language"],
                            source_id=source["source_id"],heading_index=index,evidence_ids=[proof])
            graph["entities"].append(entity.model_dump()); local.append(entity)
            relationship(identifier,"DOCUMENTED_BY",source["source_id"],proof,"explicit_heading")
            for alias in aliases:
                graph["aliases"].append(Alias(alias_id=key("alias_",identifier,alias),entity_id=identifier,
                                             label=alias,language=source["language"],evidence_ids=[proof]).model_dump())
        by_label = {}
        for entity in local:
            by_label.setdefault(entity.label,[]).append(entity)
        cursor = 0
        for paragraph in source["content"].split("\n\n"):
            checkpoint()
            start, cursor = cursor, cursor+len(paragraph)+2
            if len(paragraph) > 500:
                continue
            for predicate,verbs in VERBS.items():
                left, separator, right = paragraph.partition(" "+verbs[source["language"]]+" ")
                if not separator:
                    continue
                right = right[:-1] if right.endswith(".") else right
                subjects, targets = by_label.get(left,[]), by_label.get(right,[])
                if len(subjects) != 1 or len(targets) != 1:
                    continue
                if len(graph["relationships"]) >= MAX_RELATIONSHIPS:
                    graph["truncated"] = True
                    break
                if statement_supported(paragraph,predicate,subjects[0],targets[0]):
                    proof = evidence(source,start,start+len(paragraph),"statement")
                    relationship(subjects[0].entity_id,predicate,targets[0].entity_id,proof,"explicit_statement")
    return validate_ontology(graph,records)
