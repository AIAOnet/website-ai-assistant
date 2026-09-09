import copy
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from website_assistant.admin_auth import hash_password
from website_assistant.api import create_app
from website_assistant.build_jobs import DiscoveryJobs, StartDiscovery, JobConflict
from website_assistant.extraction import extract_page
from website_assistant.ontology_builder import build_ontology, validate_ontology, key
from website_assistant.settings import Settings

HOME="https://example.com/"
HTML='''<html lang="en"><head><title>North Garden</title></head><body><main>
<h1>Company: North Garden</h1><p>We grow and maintain plants for our local customers throughout the year.</p>
<h2>Product: Water Monitor (WM)</h2><p>The monitor measures water availability.</p>
<h2>Service: Garden Care</h2><p>The team maintains gardens.</p>
<h2>Category: Equipment</h2><p>Equipment is available for garden maintenance.</p>
<h2>Location: Stockholm</h2><p>This is our local office.</p>
<h2>Contact: Jane Doe</h2><p>A public business contact listed on the website.</p>
<p>North Garden provides Water Monitor.</p><p>North Garden provides Garden Care.</p>
<p>Water Monitor belongs to Equipment.</p><p>North Garden is located in Stockholm.</p>
</main></body></html>'''


def record(html=HTML,url=HOME):
    return extract_page({"url":url,"html":html},HOME)


class OntologyTests(unittest.TestCase):
    def test_entities_relationships_aliases_and_evidence(self):
        source=record();graph=build_ontology([source])
        self.assertEqual(len(graph["entities"]),6)
        semantic=[edge for edge in graph["relationships"] if edge["predicate"]!="DOCUMENTED_BY"]
        self.assertEqual(len(semantic),4)
        self.assertEqual({e["type"] for e in graph["entities"]},{"organization","product","service","category","location","business_contact"})
        self.assertEqual([alias["label"] for alias in graph["aliases"]],["WM"])
        for evidence in graph["evidence"]:
            self.assertEqual(source["content"][evidence["start"]:evidence["end"]],evidence["quote"])
            self.assertEqual(evidence["source_url"],HOME)
        self.assertEqual(validate_ontology(graph,[source]),graph)

    def test_general_headings_are_topics_not_guessed_products(self):
        graph=build_ontology([record(HTML.replace("Product: Water Monitor (WM)","Water Monitor"))])
        entity=next(e for e in graph["entities"] if e["label"]=="Water Monitor")
        self.assertEqual(entity["type"],"topic")
        self.assertFalse(any(e["predicate"]=="PROVIDES" and e["object_id"]==entity["entity_id"] for e in graph["relationships"]))

    def test_negative_qualified_and_partial_statements_do_not_generate_edges(self):
        for statement in ("North Garden does not provide Water Monitor.",
                          "North Garden provides Water Monitor only in summer.",
                          "It is false that North Garden provides Water Monitor.",
                          "North Garden provides Water Monitor. This is unconfirmed."):
            source=record(HTML.replace("North Garden provides Water Monitor.",statement))
            graph=build_ontology([source]);product=next(e for e in graph["entities"] if e["label"]=="Water Monitor")
            self.assertFalse(any(e["predicate"]=="PROVIDES" and e["object_id"]==product["entity_id"] for e in graph["relationships"]))

    def test_duplicate_names_are_not_merged_or_used_ambiguously(self):
        source=record(HTML.replace("</main>","<h2>Product: Water Monitor (WM)</h2><p>A different unit with the same name.</p></main>"))
        graph=build_ontology([source]);products=[e for e in graph["entities"] if e["label"]=="Water Monitor"]
        self.assertEqual(len(products),2)
        self.assertNotEqual(products[0]["entity_id"],products[1]["entity_id"])
        self.assertFalse(any(e["predicate"]=="PROVIDES" and e["object_id"] in {p["entity_id"] for p in products} for e in graph["relationships"]))
        graph=build_ontology([record(url=HOME+"a"),record(url=HOME+"b")])
        self.assertEqual(len([e for e in graph["entities"] if e["label"]=="North Garden"]),2)

    def test_ids_survive_body_edits_and_unrelated_heading_insertion(self):
        before=build_ontology([record()])
        after=build_ontology([record(HTML.replace("<main>","<main><h1>Overview</h1><p>Welcome to our website.</p>").replace("The team maintains gardens.","The team maintains local gardens."))])
        ids={e["label"]:e["entity_id"] for e in after["entities"]}
        for entity in before["entities"]:
            self.assertEqual(ids[entity["label"]],entity["entity_id"])

    def test_evidence_and_schema_tampering_rejected(self):
        source=record();original=build_ontology([source])
        modifications=[lambda g:g["evidence"][0].update(quote="Invented text"),
                       lambda g:g["entities"][0].update(type="product"),
                       lambda g:g["aliases"][0].update(label="Invented alias"),
                       lambda g:g["relationships"][0].update(object_id="missing"),
                       lambda g:g["relationships"][0].update(predicate="BOOK_MEETING"),
                       lambda g:g["evidence"][0].update(source_url="https://elsewhere.example/")]
        for change in modifications:
            graph=copy.deepcopy(original);change(graph)
            with self.assertRaises(ValueError):
                validate_ontology(graph,[source])

    def test_changed_or_removed_sources_invalidate_graph(self):
        graph=build_ontology([record()])
        with self.assertRaises(ValueError):
            validate_ontology(graph,[])
        with self.assertRaises(ValueError):
            validate_ontology(graph,[record(HTML.replace("The team maintains gardens.","The team maintains local gardens."))])

    def test_validator_rejects_ambiguous_relationship_target(self):
        source=record(HTML.replace("</main>","<h2>Product: Water Monitor (WM)</h2><p>Another unit with the same name.</p></main>"))
        graph=build_ontology([source])
        subject=next(e for e in graph["entities"] if e["label"]=="North Garden")
        target=next(e for e in graph["entities"] if e["label"]=="Water Monitor")
        quote="North Garden provides Water Monitor."
        start=source["content"].index(quote);end=start+len(quote)
        proof=key("ev_",source["source_id"],str(start),str(end),"statement")
        graph["evidence"].append({"evidence_id":proof,"source_id":source["source_id"],"source_url":HOME,
                                  "source_checksum":source["checksum"],"start":start,"end":end,"quote":quote,"kind":"statement"})
        graph["relationships"].append({"relationship_id":key("rel_",subject["entity_id"],"PROVIDES",target["entity_id"],proof),
            "subject_id":subject["entity_id"],"predicate":"PROVIDES","object_id":target["entity_id"],"evidence_ids":[proof],
            "extraction_method":"explicit_statement","review_status":"automated"})
        with self.assertRaises(ValueError):
            validate_ontology(graph,[source])

    def test_empty_sparse_and_truncated_graphs_are_explicit(self):
        self.assertEqual(build_ontology([])["entities"],[])
        source=record();source.pop("sections")
        self.assertEqual(build_ontology([source])["entities"],[])
        html='<html lang="en"><body><main>'+"".join(f'<h2>Topic {i}</h2><p>We provide information for our customers and the local community.</p>' for i in range(45))+'</main></body></html>'
        graph=build_ontology([record(html)])
        self.assertTrue(graph["truncated"])
        self.assertEqual(len(graph["entities"]),40)

    def test_swedish_explicit_relationships(self):
        html='<html lang="sv"><body><main><h1>Företag: Nord</h1><p>Vi har produkter för våra kunder och hjälper med utrustning.</p><h2>Tjänst: Skötsel</h2><p>Nord erbjuder Skötsel.</p></main></body></html>'
        graph=build_ontology([record(html)])
        self.assertEqual(len([e for e in graph["relationships"] if e["predicate"]=="PROVIDES"]),1)
        self.assertTrue(all(e["language"]=="sv" for e in graph["entities"]))


class OntologyJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password="ontology-test-password"
        cls.password_hash=hash_password(cls.password)

    def test_saved_graph_and_authenticated_inspection(self):
        class Crawler:
            def __init__(self,limits):self.limits=limits
            def run(self,homepage,**kwargs):
                return {"homepage":homepage,"canonical_homepage":homepage,"pages":[{"url":homepage,"html":HTML,"depth":0}],
                        "skipped":[],"status":"complete","page_count":1,"requests":1}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with TestClient(create_app(Settings(data_path=root,admin_username="viewer",admin_password_hash=self.password_hash,
                                                admin_role="viewer",admin_cookie_secure=False))) as client:
                jobs=client.app.state.discovery_jobs;jobs.crawler_factory=Crawler
                job=jobs.start(StartDiscovery(homepage=HOME));jobs.wait()
                endpoint="/api/admin/discovery/"+job["id"]+"/ontology"
                self.assertEqual(client.get(endpoint).status_code,401)
                client.post("/api/admin/login",json={"username":"viewer","password":self.password})
                response=client.get(endpoint)
                self.assertEqual(response.status_code,200)
                self.assertEqual(len(response.json()["entities"]),6)
                self.assertEqual(jobs.get(job["id"])["report"]["ontology"]["validation"],"passed")
                self.assertFalse((root/"sources.json").exists())
                self.assertEqual(client.app.state.contacts,[])
                self.assertEqual(client.app.state.ontology["entities"],[])
                graph_path=root/"builds"/job["id"]/"ontology.json"
                graph=json.loads(graph_path.read_text());graph["evidence"][0]["quote"]="Tampered"
                graph_path.write_text(json.dumps(graph))
                self.assertEqual(client.get(endpoint).status_code,409)

    def test_unknown_and_unfinished_jobs_cannot_load_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs=DiscoveryJobs(Path(directory))
            with self.assertRaises(KeyError):
                jobs.ontology("../outside")
            with self.assertRaises(KeyError):
                jobs.ontology("0"*32)
