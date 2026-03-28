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
st.set_page_config(page_title="Dot Ingest", layout="wide")
st.title("Dot Ingest (URL-only)")
st.caption("Paste a URL. Threads work best via ThreadReaderApp unroll links; single tweets are best-effort.")


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


def is_threadreader_url(u: str) -> bool:
    return (urlparse(u).netloc or "").lower().endswith("threadreaderapp.com")


def extract_visible_text_from_html(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    text = container.get_text(separator="\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def strip_link_media_residue(text: str) -> str:
    text = re.sub(r"(?im)^\shttps?://t.co/\S+\s$", "", text)
    text = re.sub(r"(?im)^\spic.twitter.com/\S+\s$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def extract_threadreader_text(html: str) -> str:
    """
    Slice ThreadReaderApp page down to just the thread text (strip UI, donate, crypto, etc.).
    Thread Reader App pages include extra UI around the thread. 
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body or soup
    raw = main.get_text(separator="\n")
    raw = re.sub(r"[ \t]+\n", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    # Start anchor: first substantive thread line if present (works for this class of threads)
    start_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("My dear front-end developers"):
            start_idx = i
            break
        if ln.startswith("1/"):
            start_idx = i
            break

    # Fallback start: after the "tweets / read" header area
    if start_idx is None:
        for i, ln in enumerate(lines):
            if re.search(r"\b\d+\s+tweets\b", ln):
                start_idx = min(i + 1, len(lines) - 1)
                break

    if start_idx is None:
        start_idx = 0

    # End anchors: promo/support blocks
    end_markers = [
        "Keep Current with",
        "Stay in touch and get notified",
        "This Thread may be Removed Anytime",
        "Try unrolling a thread yourself",
        "Did Thread Reader help you today",
        "Become a Premium Member",
        "Donate via Paypal",
        "Or Donate anonymously using crypto",
        "Email the whole thread",
        "Support us!",
    ]

    end_idx = None
    for i in range(start_idx, len(lines)):
        if any(lines[i].startswith(m) for m in end_markers):
            end_idx = i
            break
    if end_idx is None:
        end_idx = len(lines)

    core = lines[start_idx:end_idx]

    # Remove remaining UI fragments + crypto address lines
    kill_exact = {"×", "Post", "Share", "Email", "Send Email!", "Follow Us!", "copy"}
    kill_contains = [
        "How to get URL link on X",
        "Copy Link to Tweet",
        "Paste it above and click",
        "More info at",
        "Twitter Help",
        "@threadreaderapp unroll",
        "Enter URL or ID to Unroll",
        "Read on X",
        "Scrolly",
        "force a refresh",
    ]

    cleaned = []
    for ln in core:
        if ln in kill_exact:
            continue
        if any(s.lower() in ln.lower() for s in kill_contains):
            continue
        if re.match(r"^0x[a-fA-F0-9]{40}$", ln):  # Ethereum-like
            continue
        if re.match(r"^[13]{25,34}$", ln):  # Bitcoin-like
            continue
        cleaned.append(ln)

    out = "\n".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


def fetch_html(u: str, timeout: int = 20):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DotIngest/1.0)"}
    r = requests.get(u, headers=headers, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    meta = {
        "final_url": r.url,
        "content_type": r.headers.get("content-type", ""),
        "status_code": r.status_code,
    }
    return r.text, meta


def fetch_text_from_url(u: str):
    html, meta = fetch_html(u)
    ct = (meta.get("content_type") or "").lower()
    if "application/pdf" in ct:
        raise ValueError("URL looks like a PDF. Download and upload the PDF instead.")

    if is_threadreader_url(u):
        text = extract_threadreader_text(html)
    else:
        text = extract_visible_text_from_html(html)
        text = strip_link_media_residue(text)

    return normalize(text), meta


def fetch_single_tweet_via_oembed(x_url: str, timeout: int = 15) -> str:
    """
    Best-effort for public tweets without X API token.
    Uses publish.twitter.com oEmbed endpoint to get embedded HTML; then extracts text.
    """
    endpoint = "https://publish.twitter.com/oembed"
    r = requests.get(endpoint, params={"url": x_url, "omit_script": "true"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    html = data.get("html", "")
    if not html:
        return ""
    text = extract_visible_text_from_html(html)
    text = strip_link_media_residue(text)
    return normalize(text)


def build_prompt(url, state, intent, confidence, body):
    return "\n".join([
        "Dot: Librarian Ingest.",
        "",
        f"Input: {url}",
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


def build_source_md(title, url, author, published, state, intent, confidence, ingested_at, body, extracted_via):
    return "\n".join([
        f"# S_SOURCE: {title}",
        "",
        f"URL: {url}",
        f"Author: {author or 'unknown'}",
        f"Published: {published or 'unknown'}",
        f"State: {state}",
        f"Intent: {intent}",
        f"Confidence: {confidence}",
        f"Ingested_at: {ingested_at}",
        f"Extracted_via: {extracted_via}",
        "",
        "---",
        "",
        body
    ])


# =========================
# Session State
# =========================
if "generated" not in st.session_state:
    st.session_state.generated = False
if "prompt" not in st.session_state:
    st.session_state.prompt = ""
if "source_md" not in st.session_state:
    st.session_state.source_md = ""
if "payload_str" not in st.session_state:
    st.session_state.payload_str = ""
if "extracted_text" not in st.session_state:
    st.session_state.extracted_text = ""
if "effective_url" not in st.session_state:
    st.session_state.effective_url = ""
if "url_prefilled_once" not in st.session_state:
    st.session_state.url_prefilled_once = False
if "url_prefill" not in st.session_state:
    st.session_state.url_prefill = ""

# Prefill URL from iOS Shortcut (?url=...)
try:
    params = st.query_params
    incoming = params.get("url", None)
    if incoming and not st.session_state.url_prefilled_once:
        st.session_state.url_prefill = incoming
        st.session_state.url_prefilled_once = True
except Exception:
    pass


# =========================
# UI
# =========================
url_main = st.text_input(
    "URL",
    value=st.session_state.get("url_prefill", ""),
    placeholder="Paste X link or ThreadReaderApp unrolled link here..."
)

with st.sidebar:
    st.header("Metadata (optional)")
    title = st.text_input("Title", value="Ingested Source")
    author = st.text_input("Author (optional)")
    published = st.text_input("Published date/time (optional)")
    state = st.selectbox("Source state", ["Unread", "In-Progress", "Ingested", "Archived"], index=0)
    intent = st.selectbox("Intent shelf", ["Thesis-Building", "Mental R&D", "Decision Support", "Capability Building", "Challenge", "Context"], index=0)
    confidence = st.selectbox("Confidence", ["Provisional", "Validated"], index=0)

fetch_clicked = st.button("Fetch & Generate", type="primary")

if fetch_clicked:
    st.session_state.generated = False
    st.session_state.extracted_text = ""
    st.session_state.effective_url = ""

    u = (url_main or "").strip()
    if not u:
        st.error("Please paste a URL.")
        st.stop()

    try:
        # Case 1: ThreadReader unrolled page (best path for full threads)
        if is_threadreader_url(u):
            text, meta = fetch_text_from_url(u)
            if len(text) < 300:
                raise ValueError("ThreadReader page text was unexpectedly short.")
            st.session_state.extracted_text = text
            st.session_state.effective_url = u

        # Case 2: X URL (single tweet best-effort)
        elif is_x_url(u):
            text = fetch_single_tweet_via_oembed(u)
            if len(text) < 200:
                st.error("Could not reliably extract this X link directly.")
                st.info(
                    "For full threads (recommended):\n"
                    "1) In X, reply to the thread with:\n"
                    "   @threadreaderapp unroll\n"
                    "2) Open the bot’s threadreaderapp.com/thread/... link\n"
                    "3) Share → Dot Ingest (this shortcut)\n"
                    "Then Fetch again here.\n\n"
                    "Thread Reader App documents this bot unroll workflow and its constraints. "
                )
                st.stop()

            st.session_state.extracted_text = text
            st.session_state.effective_url = "publish.twitter.com/oembed"

        # Case 3: Normal web URL
        else:
            text, meta = fetch_text_from_url(u)
            if len(text) < 200:
                raise ValueError("Extracted content is too short to be useful.")
            st.session_state.extracted_text = text
            st.session_state.effective_url = meta.get("final_url", u)

        # Build outputs
        ingested_at = datetime.date.today().isoformat()

        source_md = build_source_md(
            title=title,
            url=u,
            author=author,
            published=published,
            state=state,
            intent=intent,
            confidence=confidence,
            ingested_at=ingested_at,
            body=st.session_state.extracted_text,
            extracted_via=st.session_state.effective_url
        )

        payload = {
            "schema_version": "1.0",
            "generated_at": datetime.datetime.now().isoformat(),
            "source": {
                "title": title,
                "url": u,
                "author": author,
                "published": published or "unknown",
                "ingested_at": ingested_at,
                "state": state,
                "intent": intent,
                "confidence": confidence,
                "extracted_via": st.session_state.effective_url,
                "content": st.session_state.extracted_text
            }
        }

        prompt = build_prompt(u, state, intent, confidence, st.session_state.extracted_text)

        st.session_state.source_md = source_md
        st.session_state.payload_str = json.dumps(payload, indent=2)
        st.session_state.prompt = prompt
        st.session_state.generated = True

    except Exception as e:
        st.error(str(e))
        st.stop()


# =========================
# Outputs
# =========================
if st.session_state.generated:
    st.markdown("---")
    st.subheader("Preview")
    st.text_area("Extracted text", st.session_state.extracted_text, height=260)
    st.caption(f"Extracted via: {st.session_state.effective_url}")

    st.subheader("Downloads")
    c1, c2 = st.columns(2)
    with c1:
        st.download_button("Download source.md", st.session_state.source_md, file_name="source.md", mime="text/markdown")
    with c2:
        st.download_button("Download dot_payload.json", st.session_state.payload_str, file_name="dot_payload.json", mime="application/json")

    st.subheader("Ingestion Prompt (tap to select & copy)")
    st.code(st.session_state.prompt, language="text")
    st.info("On iPhone: long-press in the code block → Select All → Copy.")
