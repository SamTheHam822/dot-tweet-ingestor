import re
import json
import datetime
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup
import streamlit as st

st.set_page_config(page_title="Dot URL Ingestor", layout="wide")

st.title("Dot URL Ingestor (v1 — URL-only)")
st.caption("Paste a URL. The app fetches and extracts text, then exports Dot-ready artifacts.")

# -----------------------------
# Helpers
# -----------------------------
def normalize(t: str) -> str:
    t = t.replace("\r\n", "\n").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t

def extract_visible_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # Remove scripts/styles/nav/footer to reduce noise
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()

    # Prefer <article> if it exists
    article = soup.find("article")
    container = article if article else soup.body if soup.body else soup

    text = container.get_text(separator="\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text

def is_x_url(u: str) -> bool:
    host = (urlparse(u).netloc or "").lower()
    return host.endswith("x.com") or host.endswith("twitter.com")

def fetch_x_oembed_text(u: str) -> str:
    """
    Best-effort: Use official publish.twitter.com oEmbed endpoint to get the embedded HTML.
    This often works for public tweets without login, but not guaranteed.
    Returns extracted visible text.
    """
    endpoint = "https://publish.twitter.com/oembed"
    r = requests.get(endpoint, params={"url": u, "omit_script": "true"}, timeout=20)
    r.raise_for_status()
    data = r.json()
    html = data.get("html", "")
    if not html:
        raise ValueError("oEmbed returned no HTML.")
    return extract_visible_text_from_html(html)

def fetch_url_text(u: str) -> tuple[str, dict]:
    """
    Fetch URL and extract text. Returns (text, meta).
    meta includes content-type and final_url.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; DotURLIngestor/1.0)"
    }
    r = requests.get(u, headers=headers, timeout=25, allow_redirects=True)
    r.raise_for_status()

    content_type = (r.headers.get("content-type") or "").lower()
    final_url = r.url

    if "application/pdf" in content_type:
        raise ValueError("URL appears to be a PDF. Please download and upload the PDF for ingestion.")

    if "text/html" not in content_type and "<html" not in r.text.lower():
        raise ValueError(f"Unsupported content-type for auto extraction: {content_type}")

    text = extract_visible_text_from_html(r.text)
    return text, {"content_type": content_type, "final_url": final_url}

def build_source_md(source_id, title, published, url, author, state, intent, confidence, ingested_at, body):
    return "\n".join([
        f"# {source_id}: {title} (Web, Publish {published or 'unknown'})",
        "",
        f"**URL:** {url or 'n/a'}",
        f"**Author:** {author or 'unknown'}",
        f"**State:** {state}",
        f"**Intent:** {intent}",
        f"**Confidence:** {confidence}",
        f"**Ingested_at:** {ingested_at}",
        "",
        "---",
        "",
        "## Captured text",
        "",
        body,
        "",
        "---",
        "",
        "## Notes",
        "- Auto-extracted from URL. Validate claims against source when needed."
    ])

def build_prompt(url, state, intent, confidence, body):
    return "\n".join([
        "Dot: Librarian Ingest.",
        "",
        f"Input: {url or '[paste source link]'}",
        f"State: {state}",
        f"Intent shelf: {intent}",
        f"Confidence: {confidence}",
        "",
        "Rules:",
        "- Create/Update Source node with publish date if known.",
        "- Extract Concepts (C#) and Claims (K#) ONLY grounded in the captured text.",
        "- Mark anything else PROVISIONAL.",
        "- Merge duplicates; enforce naming conventions.",
        "",
        "Captured text:",
        "---",
        body
    ])

# -----------------------------
# Session state (persist outputs across downloads/reruns)
# -----------------------------
for key, default in {
    "generated": False,
    "source_md": "",
    "payload_str": "",
    "prompt": "",
    "extracted_text": "",
    "last_error": ""
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# -----------------------------
# UI
# -----------------------------
st.subheader("1) Paste URL")
url_main = st.text_input(
    "URL",
    placeholder="https://x.com/...  or  https://example.com/article"
)

with st.sidebar:
    st.header("Metadata (optional)")
    author = st.text_input("Author (optional)")
    published = st.text_input("Published date/time (optional)")
    ingested_at = datetime.date.today().isoformat()

    intent = st.selectbox(
        "Intent shelf",
        ["Thesis-Building", "Mental R&D", "Decision Support", "Capability Building", "Challenge", "Context"],
        index=0
    )
    state = st.selectbox("Source state", ["Unread", "In-Progress", "Ingested", "Archived"], index=0)
    confidence = st.selectbox("Confidence", ["Provisional", "Validated"], index=0)
    title = st.text_input("Title", value="URL Capture")

st.subheader("2) Fetch + Extract")
fetch_clicked = st.button("Fetch text from URL", type="primary")

if fetch_clicked:
    st.session_state.last_error = ""
    st.session_state.extracted_text = ""
    st.session_state.generated = False

    if not url_main.strip():
        st.session_state.last_error = "Please paste a URL first."
    else:
        u = url_main.strip()
        try:
            # Special handling for X URLs
            if is_x_url(u):
                extracted = fetch_x_oembed_text(u)
                st.session_state.extracted_text = normalize(extracted)
            else:
                extracted, meta = fetch_url_text(u)
                st.session_state.extracted_text = normalize(extracted)
        except Exception as e:
            st.session_state.last_error = str(e)

if st.session_state.last_error:
    st.error(st.session_state.last_error)
    st.info(
        "If this is an X (Twitter) link and extraction fails, X may be blocking access. "
        "Tier 2 can add an X API token for reliable URL-only extraction."
    )

if st.session_state.extracted_text:
    st.subheader("3) Review extracted text (optional)")
    st.text_area("Extracted text", st.session_state.extracted_text, height=220)

    gen_clicked = st.button("Generate Dot outputs")

    if gen_clicked:
        body = st.session_state.extracted_text
        source_id = "S_URL"

        source_md = build_source_md(
            source_id=source_id,
            title=title,
            published=published,
            url=url_main.strip(),
            author=author,
            state=state,
            intent=intent,
            confidence=confidence,
            ingested_at=ingested_at,
            body=body
        )

        payload = {
            "schema_version": "0.1",
            "generated_at": datetime.datetime.now().isoformat(),
            "source": {
                "id": source_id,
                "type": "Source",
                "title": title,
                "url": url_main.strip(),
                "author": author,
                "published": published or "unknown",
                "ingested_at": ingested_at,
                "state": state,
                "intent": intent,
                "confidence": confidence,
                "content": body
            },
            "nodes": [
                {"id": source_id, "type": "S", "name": f"{title} (Web, Publish {published or 'unknown'})"}
            ],
            "edges": [],
            "placeholders": {"concepts": [], "claims": [], "frameworks": [], "work_artifacts": []}
        }

        prompt = build_prompt(url_main.strip(), state, intent, confidence, body)

        st.session_state.source_md = source_md
        st.session_state.payload_str = json.dumps(payload, indent=2)
        st.session_state.prompt = prompt
        st.session_state.generated = True

# -----------------------------
# Outputs (persist after download)
# -----------------------------
if st.session_state.generated:
    st.markdown("---")
    st.subheader("Outputs")

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "Download source.md",
            data=st.session_state.source_md,
            file_name="source.md",
            mime="text/markdown",
            key="dl_source"
        )
    with c2:
        st.download_button(
            "Download dot_payload.json",
            data=st.session_state.payload_str,
            file_name="dot_payload.json",
            mime="application/json",
            key="dl_json"
        )

    st.subheader("Ingestion Prompt (tap to select & copy)")
    st.code(st.session_state.prompt, language="text")
    st.info("On iPhone: long-press in the code block → Select All → Copy.")

st.markdown("---")
st.caption("v1 URL-only. For X threads reliably, add Tier 2 (X API token in Streamlit secrets).")
