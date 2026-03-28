import re
import json
import datetime
from urllib.parse import urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
import streamlit as st


# =========================
# App Config
# =========================
st.set_page_config(page_title="Dot URL Ingestor", layout="wide")
st.title("Dot URL Ingestor (v4 — URL-only, Thread-capable, UnrollNow + Multi-Unroll)")
st.caption(
    "Paste a URL. For X/Twitter links, Auto mode tries UnrollNow (URL replacement) first, then other unroll endpoints, "
    "skips JS loading-shell pages, then falls back to direct fetch."
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

def is_threadreader_url(u: str) -> bool:
    """
    Thread Reader App bot returns an unrolled page on threadreaderapp.com, which we can fetch like a normal article. 
    """
    return "threadreaderapp.com" in (urlparse(u).netloc or "").lower()

def extract_tweet_id(u: str):
    m = re.search(r"/status/(\d+)", urlparse(u).path)
    return m.group(1) if m else None


def unrollnow_url_from_x(u: str) -> str:
    """
    UnrollNow supports unrolling by replacing 'x.com' with 'unrollnow.com' in the address bar.
    We implement that deterministic transformation here.
    """
    p = urlparse(u)
    # Normalize host
    host = p.netloc.lower()
    if host.endswith("twitter.com"):
        new_netloc = "unrollnow.com"
    elif host.endswith("x.com"):
        new_netloc = "unrollnow.com"
    else:
        new_netloc = p.netloc

    # Keep path + query so the unroller can identify the tweet/thread
    return urlunparse((p.scheme or "https", new_netloc, p.path, p.params, p.query, p.fragment))


def build_unroll_candidates(x_url: str):
    """
    Provider stack (unroll-first):
    1) UnrollNow deterministic URL replacement (true URL-only)
    2) twitter-thread.com deterministic endpoints (often helpful, but sometimes JS shells) 
    """
    tweet_id = extract_tweet_id(x_url)

    candidates = [
        {"name": "unrollnow.com (replace host)", "url": unrollnow_url_from_x(x_url)},
    ]

    if tweet_id:
        candidates.extend([
            {"name": "twitter-thread.com (t)", "url": f"https://twitter-thread.com/t/{tweet_id}"},
            {"name": "twitter-thread.com (unroll)", "url": f"https://twitter-thread.com/unroll/{tweet_id}"},
        ])

    return candidates


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


def looks_like_js_loading_shell(text: str) -> bool:
    """
    Detect pages that are basically JS-driven loading shells, e.g.:
    'Unrolling your thread... Gathering the Tweets...' 
    """
    t = (text or "").lower()
    signals = [
        "unrolling your thread",
        "gathering the tweets",
        "unrolling thread",
        "gathering tweets",
        "unrolling your thread...",
        "gathering the tweets...",
    ]
    return any(s in t for s in signals)


def fetch_html(u: str):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; DotURLIngestor/4.0)"}
    r = requests.get(u, headers=headers, timeout=25, allow_redirects=True)
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
    
    # Special-case Thread Reader App pages
    if is_threadreader_url(u):
        text = extract_threadreader_text(html)
    else:
        text = extract_visible_text_from_html(html)
        text = strip_link_media_residue(text)

    return text, meta

def try_unroll_stack(x_url: str, min_chars: int = 400):
    """
    Try multiple unroll candidates and return:
    (best_text, used_url, debug_log)
    """
    debug = []
    candidates = build_unroll_candidates(x_url)

    for cand in candidates:
        name = cand["name"]
        url = cand["url"]
        try:
            text, meta = fetch_text_from_url(url)
            debug.append(f"✅ {name}: fetched {len(text)} chars from {url}")

            if looks_like_js_loading_shell(text):
                debug.append(f"⚠️ {name}: detected JS loading shell; skipping")
                continue

            if len(text.strip()) < min_chars:
                debug.append(f"⚠️ {name}: too short (<{min_chars}); skipping")
                continue

            return text, url, debug

        except Exception as e:
            debug.append(f"❌ {name}: error: {str(e)}")

    return "", "", debug


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
        "- URL-only thread ingestion using UnrollNow + fallback unroll endpoints. ",
        "- Treat claims as Provisional unless validated against the source.",
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

def is_threadreader_url(u: str) -> bool:
    return (urlparse(u).netloc or "").lower().endswith("threadreaderapp.com")

def extract_threadreader_text(html: str) -> str:
    """
    Extract only the actual thread text from a Thread Reader App page by slicing
    between clear start/end anchors, then apply light cleanup.

    Thread Reader App pages include extensive UI (share/unroll/help/subscribe/donate). 
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove noisy tags
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body or soup
    raw = main.get_text(separator="\n")

    # Normalize whitespace early
    raw = re.sub(r"[ \t]+\n", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()

    # Split into lines for slicing
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]  # drop blanks

    # -------------------------
    # Anchor-based slicing
    # -------------------------
    # END markers (footer/promotions start)
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
    ]

    # START markers:
    # Prefer the actual thread opening line if present.
    # If not, fall back to the first substantive paragraph after the header block.
    start_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("My dear front-end developers"):
            start_idx = i
            break

    # Fallback start: after the "tweets / read" header area (e.g., "11 tweets", "5 min read")
    if start_idx is None:
        for i, ln in enumerate(lines):
            if re.search(r"\b\d+\s+tweets\b", ln):
                # start a few lines after the header
                start_idx = min(i + 1, len(lines) - 1)
                break

    if start_idx is None:
        start_idx = 0  # last resort

    end_idx = None
    for i in range(start_idx, len(lines)):
        ln = lines[i]
        if any(ln.startswith(m) for m in end_markers):
            end_idx = i
            break

    if end_idx is None:
        end_idx = len(lines)

    core = lines[start_idx:end_idx]

    # -------------------------
    # Line-level cleanup
    # -------------------------
    # Remove stray UI glyph lines like '×' and common instruction fragments
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
    ]

    cleaned = []
    for ln in core:
        if ln in kill_exact:
            continue
        if any(s.lower() in ln.lower() for s in kill_contains):
            continue
        # Strip obvious crypto address lines (defensive)
        if re.match(r"^0x[a-fA-F0-9]{40}$", ln):  # Ethereum-like
            continue
        if re.match(r"^[13]{25,34}$", ln):  # Bitcoin-like
            continue
        cleaned.append(ln)

    out = "\n".join(cleaned)
    out = re.sub(r"\n{3,}", "\n\n", out).strip()
    return out


# =========================
# Session state
# =========================
defaults = {
    "generated": False,
    "source_md": "",
    "payload_str": "",
    "prompt": "",
    "extracted_text": "",
    "last_error": "",
    "effective_url": "",
    "debug_log": [],
}
for k, v in defaults.items():
    if k not in st.session_state:
        st.session_state[k] = v


# =========================
# UI
# =========================
st.subheader("1) Paste URL")
url_main = st.text_input("URL", placeholder="https://x.com/...")

mode = st.radio(
    "Mode",
    ["Auto (unroll-first → fallback)", "Force thread (unroll only)", "Force direct (no unroll)"],
    index=0
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

    show_debug = st.checkbox("Show debug log", value=True)


st.subheader("2) Fetch + Extract")
fetch_clicked = st.button("Fetch", type="primary")

if fetch_clicked:
    st.session_state.last_error = ""
    st.session_state.extracted_text = ""
    st.session_state.generated = False
    st.session_state.effective_url = ""
    st.session_state.debug_log = []

    if not url_main.strip():
        st.session_state.last_error = "Please paste a URL first."
    else:
        u = url_main.strip()
        effective = u

        try:
            if is_threadreader_url(u):
                # Already-unrolled thread page → fetch directly like an article page. 
                text, meta = fetch_text_from_url(effective)

            # Existing X / unroll logic stays exactly as-is below
            elif is_x_url(u):
                if mode.startswith("Force direct"):
                    effective = u
                    text, meta = fetch_text_from_url(effective)
                    if len(text.strip()) < 200:
                        raise ValueError("Direct fetch returned too little text (likely blocked).")

                else:
                    text, used_url, debug = try_unroll_stack(u)
                    st.session_state.debug_log.extend(debug)

                    if mode.startswith("Force thread"):
                        if not text:
                            raise ValueError("Thread extraction failed across all unroll providers (including UnrollNow).")
                        effective = used_url
                    else:
                        if text:
                            effective = used_url
                        else:
                            st.session_state.debug_log.append("↩️ Auto fallback: unroll failed; trying direct fetch")
                            effective = u
                            text, meta = fetch_text_from_url(effective)

                            if looks_like_js_loading_shell(text) or len(text.strip()) < 200:
                                raise ValueError("Could not extract thread text (unroll failed and direct fetch is blocked/empty).")

            else:
                text, meta = fetch_text_from_url(effective)
                if len(text.strip()) < 200:
                    raise ValueError("Fetched content is too short to be useful.")

            st.session_state.effective_url = effective
            st.session_state.extracted_text = normalize(text)

        except Exception as e:
            st.session_state.last_error = str(e)


if st.session_state.last_error:
    st.error(st.session_state.last_error)
    st.info(
        "This URL-only thread method depends on third-party unrollers. "
        "If it fails, try again later or use 'Force direct' for a single-post capture."
    )

if show_debug and st.session_state.debug_log:
    st.subheader("Debug log")
    st.code("\n".join(st.session_state.debug_log), language="text")


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
            "schema_version": "0.4",
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
    "UnrollNow supports URL unrolling by replacing 'x.com' with 'unrollnow.com' in the address bar.  "
    "We try that first, then fallback unroll endpoints, then direct fetch."
)
