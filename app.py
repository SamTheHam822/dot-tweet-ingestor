import re
import json
import datetime
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
import streamlit as st


# =========================
# App Config
# =========================
st.set_page_config(page_title="Dot URL Ingestor", layout="wide")
st.title("Dot URL Ingestor (v2 — URL-only, Thread-capable)")
st.caption(
    "Paste a URL. If it's an X/Twitter link, the app tries to fetch the full thread via an unroll endpoint first, then falls back."
)


# =========================
# Helpers
# =========================
def normalize(t: str) -> str:
    t = t.replace("\r\n", "\n").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t


def is_x_url(u: str) -> bool:
    host = (urlparse(u).netloc or "").lower()
    return host.endswith("x.com") or host.endswith("twitter.com")


def extract_tweet_id(u: str) -> str | None:
    """
    Extract tweet_id from URLs like:
    https://x.com/{user}/status/{id}
    https://twitter.com/{user}/status/{id}
    """
    path = urlparse(u).path
    m = re.search(r"/status/(\d+)", path)
    return m.group(1) if m else None


def build_unroll_url(tweet_id: str) -> str:
    """
    Provider 1: deterministic unroll endpoint pattern.
    Example: https://twitter-thread.com/unroll/<tweet_id> 
    """
    return f"https://twitter-thread.com/unroll/{tweet_id}"


def extract_visible_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")

    # Remove common noise
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()

    # Prefer <article> or <main> if present
    container = soup.find("article") or soup.find("main") or soup.body or soup
    text = container.get_text(separator="\n")

    # Cleanup whitespace
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text


def strip_link_media_residue(text: str) -> str:
    """
    Remove common shortlink/media lines that add noise for concept extraction.
    """
    # Remove t.co lines
    text = re.sub(r"(?im)^\shttps?://t.co/\S+\s$", "", text)
    # Remove pic.twitter.com lines
    text = re.sub(r"(?im)^\spic.twitter.com/\S+\s$", "", text)
    # Collapse blanks again
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def fetch_html(u: str) -> tuple[str, dict]:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DotURLIngestor/2.0)"}
    r = requests.get(u, headers=headers, timeout=25, allow_redirects=True)
    r.raise_for_status()
    meta = {
        "final_url": r.url,
        "content_type": r.headers.get("content-type", ""),
        "status_code": r.status_code,
    }
    return r.text, meta


def fetch_text_from_url(u: str) -> tuple[str, dict]:
    html, meta = fetch_html(u)

    # Basic content-type guard (many sites omit text/html correctly; keep soft)
    ct = (meta.get("content_type") or "").lower()
    if "application/pdf" in ct:
        raise ValueError("URL appears to be a PDF. Please download and upload the PDF instead of URL ingestion.")

    text = extract_visible_text_from_html(html)
    text = strip_link_media_residue(text)
    return text, meta


def build_source_md(source_id, title, published, url, author, state, intent, confidence, ingested_at, body, extraction_url):
    return "\n".join([
        f"# {source_id}: {title} (Thread, Publish {published or 'unknown'})",
        "",
        f"URL: {url or 'n/a'}",
        f"Author: {author or 'unknown'}",
        f"State: {state}",
        f"Intent: {intent}",
        f"Confidence: {confidence}",
        f"Ingested_at: {ingested_at}",
        f"Extracted_via: {extraction_url or 'unknown'}",
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
        "- URL-only thread ingestion pipeline (no X API token).",
        "- Treat claims as Provisional unless validated against the source."
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


# =========================
# Session state (persist outputs across reruns/downloads)
# =========================
defaults = {
    "generated": False,
    "source_md": "",
    "payload_str": "",
    "prompt": "",
    "extracted_text": "",
    "last_error": "",
    "effective_url": "",
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# =========================
# UI
# =========================
st.subheader("1) Paste URL")
url_main = st.text_input("URL", placeholder="https://x.com/... or https://example.com/article")

mode = st.radio(
    "Mode",
    ["Auto (unroll-first → fallback)", "Force thread (unroll only)", "Force direct (no unroll)"],
    index=0,
    horizontal=False
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

    title = st.text_input("Title", value="X Thread")


st.subheader("2) Fetch + Extract")
fetch_clicked = st.button("Fetch", type="primary")

if fetch_clicked:
    st.session_state.last_error = ""
    st.session_state.extracted_text = ""
    st.session_state.generated = False
    st.session_state.effective_url = ""

    if not url_main.strip():
        st.session_state.last_error = "Please paste a URL first."
    else:
        u = url_main.strip()
        effective = u

        try:
            # X URL logic
            if is_x_url(u):
                tweet_id = extract_tweet_id(u)
                if not tweet_id:
                    raise ValueError("Could not find tweet_id in the URL. Expected /status/<id>.")

                unroll_url = build_unroll_url(tweet_id)

                if mode.startswith("Force thread"):
                    # Unroll only
                    effective = unroll_url
                    text, meta = fetch_text_from_url(effective)

                    if len(text.strip()) < 200:
                        raise ValueError("Unroll returned too little text (possibly blocked/rate-limited).")

                elif mode.startswith("Force direct"):
                    # Direct only
                    effective = u
                    text, meta = fetch_text_from_url(effective)

                else:
                    # Auto: unroll-first then fall back
                    try:
                        effective = unroll_url
                        text, meta = fetch_text_from_url(effective)
                        if len(text.strip()) < 200:
                            raise ValueError("Unroll too short")
                    except Exception:
                        effective = u
                        text, meta = fetch_text_from_url(effective)

            # Non-X URL logic
            else:
                text, meta = fetch_text_from_url(effective)

            st.session_state.effective_url = effective
            st.session_state.extracted_text = normalize(text)

        except Exception as e:
            st.session_state.last_error = str(e)


if st.session_state.last_error:
    st.error(st.session_state.last_error)
    st.info(
        "If thread extraction fails, the unroll endpoint may be down or blocked. "
        "Try again later, or switch to 'Force direct'."
    )


if st.session_state.extracted_text:
    st.subheader("3) Review extracted text (optional)")
    st.text_area("Extracted text", st.session_state.extracted_text, height=260)
    st.caption(f"Fetched via: {st.session_state.effective_url}")

    gen_clicked = st.button("Generate Dot outputs")

    if gen_clicked:
        body = st.session_state.extracted_text
        source_id = "S_THREAD"

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
            body=body,
            extraction_url=st.session_state.effective_url
        )

        payload = {
            "schema_version": "0.2",
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
                "content": body,
                "extraction_url": st.session_state.effective_url,
            },
            "nodes": [
                {"id": source_id, "type": "S", "name": f"{title} (Thread, Publish {published or 'unknown'})"}
            ],
            "edges": [],
            "placeholders": {"concepts": [], "claims": [], "frameworks": [], "work_artifacts": []}
        }

        prompt = build_prompt(url_main.strip(), state, intent, confidence, body)

        st.session_state.source_md = source_md
        st.session_state.payload_str = json.dumps(payload, indent=2)
        st.session_state.prompt = prompt
        st.session_state.generated = True


# =========================
# Outputs (persist after downloads)
# =========================
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

    st.caption(f"Fetched via: {st.session_state.effective_url}")


st.markdown("---")
st.caption(
    "Auto mode tries thread unroll first, then falls back to direct fetch. "
    "Thread unroll relies on a third-party unroll page format like /unroll/<tweet_id>. "
)
