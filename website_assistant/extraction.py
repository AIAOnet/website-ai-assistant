"""Conservative HTML-to-source extraction with explicit language and provenance."""
import hashlib
import re
from email.message import Message
from datetime import datetime, timezone
from html.parser import HTMLParser

from .crawl_policy import normalize_url
from .knowledge import INJECTION_PATTERNS, content_checksum, validate_record
from .site_settings import SiteScope

VOID = {"area","base","br","col","embed","hr","img","input","link","meta","param","source","track","wbr"}
EXCLUDED = {"script","style","nav","footer","header","aside","form","noscript","template","svg"}
BLOCK = {"p","div","section","article","li","ul","ol","tr","blockquote","pre","br"}
BOILERPLATE = re.compile(r"(?:^|[-_ ])(?:cookie|cookies|cookiebanner|cookieconsent|navigation|newsletter|breadcrumb)(?:$|[-_ ])", re.I)


class ExtractionError(ValueError):
    pass


def decode_html(body, content_type):
    header = Message(); header["content-type"] = content_type
    declared = header.get_content_charset()
    if body.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif body.startswith((b"\xff\xfe",b"\xfe\xff")):
        encoding = "utf-16"
    else:
        meta = re.search(r'<meta\b[^>]*\bcharset\s*=\s*[\"\x27]?([a-zA-Z0-9_-]+)',body[:4096].decode("ascii",errors="ignore"),re.I)
        label = (declared or (meta.group(1) if meta else "utf-8")).lower().replace("_","-")
        encoding = {"utf-8":"utf-8","utf8":"utf-8","windows-1252":"cp1252","cp1252":"cp1252",
                    "iso-8859-1":"cp1252","latin-1":"cp1252","us-ascii":"ascii","ascii":"ascii"}.get(label)
        if not encoding:
            raise ExtractionError("unsupported_character_encoding")
    try:
        return body.decode(encoding), encoding
    except UnicodeError as error:
        raise ExtractionError("invalid_character_encoding") from error


class Document(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = {"tag":"root","attrs":{},"children":[]}
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 50000 or len(self.stack) > 128:
            raise ExtractionError("html_structure_limit")
        node = {"tag":tag,"attrs":dict(attrs),"children":[]}
        self.stack[-1]["children"].append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,0,-1):
            if self.stack[index]["tag"] == tag:
                del self.stack[index:]
                return

    def handle_data(self, value):
        self.stack[-1]["children"].append(value)


def hidden(node):
    attrs = node["attrs"]
    style = re.sub(r"\s+","",attrs.get("style") or "").lower()
    return (node["tag"] in EXCLUDED or "hidden" in attrs or attrs.get("aria-hidden") == "true"
            or "display:none" in style or "visibility:hidden" in style
            or BOILERPLATE.search((attrs.get("id") or "")+" "+(attrs.get("class") or "")))


def nodes(node, tag, *, visible=False):
    if visible and hidden(node):
        return
    if node["tag"] == tag:
        yield node
        return
    for child in node["children"]:
        if isinstance(child,dict):
            yield from nodes(child,tag,visible=visible)


def text(node):
    if hidden(node):
        return ""
    return " ".join(child if isinstance(child,str) else text(child) for child in node["children"])


def normalized(value):
    return " ".join(value.split())


def language_of(declared, content):
    if declared:
        lang = declared.lower().split("-")[0].split("_")[0]
        if lang not in {"en","sv"}:
            raise ExtractionError("unsupported_language")
        return lang, "html_lang"
    words = set(re.findall(r"\w+",content.lower()))
    scores = {"en":len(words & {"the","and","our","with","for","this","is","are","we","of","from","your"}),
              "sv":len(words & {"och","för","våra","med","som","är","vi","att","av","från","vår","har"})}
    winner = max(scores,key=scores.get)
    if scores[winner] < 3 or scores[winner] - min(scores.values()) < 2:
        raise ExtractionError("unknown_language")
    return winner, "word_heuristic"


def extract_page(page, home_url):
    url = normalize_url(page["url"])
    if not SiteScope(home_url).allows(url):
        raise ExtractionError("source_out_of_scope")
    html = page["html"]
    if len(html) > 5_000_000:
        raise ExtractionError("html_size_limit")
    document = Document()
    document.feed(html)
    document.close()
    root = document.root
    preferred = list(nodes(root,"main",visible=True))
    if not preferred:
        preferred = list(nodes(root,"article",visible=True))
    if not preferred:
        preferred = list(nodes(root,"body",visible=True)) or [root]
    paragraphs, headings, buffer = [], [], []
    def flush():
        value = normalized(" ".join(buffer)); buffer.clear()
        if value:
            paragraphs.append(value)
    def visit(node):
        if hidden(node) or node["tag"] in {"head","title","meta","link"}:
            return
        if re.fullmatch(r"h[1-6]",node["tag"]):
            flush()
            label = normalized(text(node))
            if label:
                headings.append((len(paragraphs),label))
                paragraphs.append(label)
            return
        if node["tag"] in BLOCK:
            flush()
        for child in node["children"]:
            if isinstance(child,str):
                buffer.append(child)
            else:
                visit(child)
        if node["tag"] in BLOCK:
            flush()
    for node in preferred:
        visit(node); flush()
    content = "\n\n".join(paragraphs)
    if len(content) < 50 or len(content.split()) < 8:
        raise ExtractionError("insufficient_content")
    if len(content) > 100000:
        raise ExtractionError("text_size_limit")
    title_node = next(nodes(root,"title"),None)
    title = normalized(text(title_node)) if title_node else ""
    title = (title or (headings[0][1] if headings else url))[:300]
    if INJECTION_PATTERNS.search(title+"\n"+content):
        raise ExtractionError("instruction_like_content")
    html_node = next(nodes(root,"html"),None)
    language, detection = language_of((html_node["attrs"].get("lang") or "") if html_node else "",content)
    offsets, cursor = [], 0
    for paragraph in paragraphs:
        offsets.append(cursor); cursor += len(paragraph)+2
    starts = [(offsets[index], label) for index,label in headings]
    if not starts or starts[0][0] != 0:
        starts.insert(0,(0,title))
    sections = [{"kind":"section","label":label,"start":start,
                 "end":starts[i+1][0] if i+1<len(starts) else len(content)} for i,(start,label) in enumerate(starts)]
    declared = None
    for link in nodes(root,"link"):
        if "canonical" in (link["attrs"].get("rel") or "").lower().split():
            try:
                candidate = normalize_url(link["attrs"].get("href") or "",url)
                if SiteScope(home_url).allows(candidate):
                    declared = candidate
            except ValueError:
                pass
            break
    # Keep the actually fetched URL as citation identity; an HTML tag is not proof
    # that another page was fetched or contains the same evidence.
    category_words = set(re.findall(r"\w+",(title+" "+" ".join(label for _,label in headings)).lower()))
    category = "product" if category_words & {"product","products","produkt","produkter"} else (
        "service" if category_words & {"service","services","tjänst","tjänster"} else (
        "company" if category_words & {"company","företag","organisation"} else "information"))
    return validate_record({"source_id":"web_"+hashlib.sha256(url.encode()).hexdigest()[:24],
        "title":title,"canonical_url":url,"category":category,"language":language,
        "retrieved_at":page.get("fetched_at") or datetime.now(timezone.utc).isoformat(),
        "checksum":content_checksum(content),"source_status":"active","content":content,
        "sections":sections,"review_status":"automated","language_detection":detection,
        "extraction_version":1,"html_checksum":content_checksum(html),"declared_canonical_url":declared,
        "source_encoding":page.get("encoding","utf-8")})


def extract_pages(pages, home_url, *, checkpoint=lambda:None):
    records, skipped, identical, similarities = [], [], {}, []
    fingerprints = []
    for page in sorted(pages,key=lambda item:item["url"]):
        checkpoint()
        try:
            record = extract_page(page,home_url)
        except (ValueError, RecursionError) as error:
            skipped.append({"url":page["url"],"reason":str(error) if isinstance(error,ExtractionError) else "invalid_source"})
            continue
        key = (record["language"],record["checksum"])
        if key in identical:
            skipped.append({"url":page["url"],"reason":"duplicate_content","duplicate_of":identical[key]})
            continue
        identical[key] = record["source_id"]
        words = set(re.findall(r"\w+",record["content"].casefold())[:2000])
        for previous, previous_words in fingerprints:
            if record["language"] == previous["language"] and len(words & previous_words)/max(1,len(words | previous_words)) >= .95:
                similarities.append({"source_id":record["source_id"],"similar_to":previous["source_id"]})
                break  # Similar pages remain separate evidence; only exact duplicates are dropped.
        fingerprints.append((record,words))
        records.append(record)
    return records, {"status":"ready" if records else "empty","source_count":len(records),
                     "skipped":skipped,"similar_pages":similarities,"review_status":"automated"}
