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

### START: DOT WORTHINESS HELPERS ###

def dot_worthiness_score(text: str):
    """
    Returns (score_0_to_10, reasons_list).
    Primary axis: Dot Skill OS / personal learning compounding.
    Heuristic-only, based on extracted text content (no web calls).
    """
    import re

    t = (text or "").strip()
    if not t:
        return 0.0, ["No extracted text."]

    tl = t.lower()
    n_chars = len(t)
    n_lines = len([ln for ln in t.splitlines() if ln.strip()])

    # Positive signals
    has_numbers = bool(re.search(r"\b\d+(.\d+)?\b", t))
    has_steps = bool(re.search(r"(?m)^\s*(\d+.|-|\u2022)\s+", t))
    has_codeish = ("" in t) or bool(re.search(r"\b(npm|pip|git|curl|bun|sql|api|repo|install)\b", tl)) 
    has_links = ("http" in tl) or ("www." in tl) or bool(re.search(r"\bgithub.com\b", tl))
    # Density proxy: more info per line implies higher signal
    avg_line_len = n_chars / max(n_lines, 1)
    density = min(1.0, avg_line_len / 120.0)  # saturates ~120 chars/line
    
    # Clickbait / authority laundering
    clickbait_markers = [
        "finally", "insane", "must read", "this changes everything",
        "you won't believe", "secret", "shocking", "breaking", "staggering",
        "game changer", "exposed", "22k", "stars"
    ]
    authority_markers = [
        "direct from", "and team", "legendary", "genius",
        "from the creator of", "the definitive", "best practice", "best practices"
    ]
    clickbait_hits = sum(1 for m in clickbait_markers if m in tl)
    authority_hits = sum(1 for m in authority_markers if m in tl)
    
    # Score components (0–10)
    score = 0.0
    reasons = []

    # Base: content volume
    if n_chars > 2500:
        score += 2.2; reasons.append("High content volume (more substance to compound).")
    elif n_chars > 1000:
        score += 1.6; reasons.append("Moderate content volume (enough to learn from).")
    else:
        score += 0.6; reasons.append("Low content volume (likely a pointer).")
    
    # Specificity & actionability
    if has_numbers:
        score += 1.1; reasons.append("Contains concrete details (numbers/metrics).")
    if has_steps:
        score += 1.6; reasons.append("Structured steps/bullets → reusable skill pattern.")
    if has_codeish:
        score += 1.4; reasons.append("Technical/operational content → skill-installation potential.")
    if has_links:
        score += 0.5; reasons.append("Has references (good for provenance).")
    
    # Density (information per line)
    score += 1.6 * density
    if density > 0.7:
        reasons.append("High information density (low fluff).")
    elif density > 0.4:
        reasons.append("Moderate information density.")
    else:
        reasons.append("Low information density (more fluff).")
    
    # Deductions
    score -= 0.9 * clickbait_hits
    if clickbait_hits:
        reasons.append(f"Clickbait language detected ({clickbait_hits}).")
    
    score -= 0.7 * authority_hits
    if authority_hits:
        reasons.append(f"Authority-signaling without evidence ({authority_hits}).")
    
    # Clamp
    score = max(0.0, min(10.0, round(score, 1)))
    
    # Make 10 rare: require structured + verifiable substance
    if score > 9.5 and not (has_steps and (has_numbers or has_codeish) and n_chars > 1400):
        score = 9.4
        reasons.append("Capped below 10: not enough structured, verifiable substance.")
    
    return score, reasons
    
    ### START: DOT WORTHINESS LABEL ###
    
def dot_worthiness_label(score: float):
        """
        Primary axis A: Dot Skill OS / personal learning compounding (foundational).
        Returns (label, short meaning).
        """
        if score < 3:
            return "🗑️ Noise", "Not worth ingesting into Dot."
        if score < 5:
            return "📍 Pointer", "Reference-only; low compounding."
        if score < 7:
            return "🧪 Optional", "Some value, but not core skill."
        if score < 9:
            return "⚙️ Skill‑Building", "Worth ingesting for capability growth."
        if score < 10:
            return "🧠 High‑Leverage", "Strong compounding potential."
        return "🔥 Canonical", "Core Dot Skill OS asset."
    
    ### END: DOT WORTHINESS LABEL ###

    
def extract_threadreader_text(html: str) -> str:
    """
    Extract only the actual unrolled thread body from ThreadReaderApp pages,
    avoiding 'More from' / promo / donation / crypto / how-to UI blocks.
    """
    soup = BeautifulSoup(html, "lxml")

    # Remove noisy tags
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "aside", "form"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.body or soup
    raw = main.get_text(separator="\n")

    # Normalize whitespace
    raw = re.sub(r"[ \t]+\n", "\n", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw).strip()

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]

    # -------------------------
    # Identify start of main thread body
    # -------------------------
    # Prefer start after the header that includes tweet count / read time:
    # e.g. "• 11 tweets • 5 min read •"
    start_idx = None
    for i, ln in enumerate(lines):
        if re.search(r"\b\d+\s+tweets\b", ln) and ("min read" in ln.lower() or "tweets" in ln.lower()):
            # Start shortly after the header block
            start_idx = min(i + 1, len(lines) - 1)
            break

    # If not found, fall back to first occurrence of an @handle (author handle)
    if start_idx is None:
        for i, ln in enumerate(lines):
            if re.match(r"^@[\w_]{1,30}$", ln):
                start_idx = min(i + 1, len(lines) - 1)
                break

    # Last resort: use entire text
    if start_idx is None:
        start_idx = 0

    # -------------------------
    # Identify end of main thread body
    # -------------------------
    end_markers = [
        "Missing some Tweet in this thread?",
        "force a refresh",
        "Keep Current with",
        "Stay in touch and get notified",
        "This Thread may be Removed Anytime",
        "Twitter may remove this content",
        "Try unrolling a thread yourself",
        "Did Thread Reader help you today",
        "Support us!",
        "Become a Premium Member",
        "Make a small donation",
        "Donate via Paypal",
        "Or Donate anonymously using crypto",
        "Email the whole thread",
        "More from",               # KEY FIX: prevents grabbing the feed section
        "More Threads",            # defensive
        "More unrolls",            # defensive
    ]

    end_idx = len(lines)
    for i in range(start_idx, len(lines)):
        if any(lines[i].startswith(m) for m in end_markers):
            end_idx = i
            break

    core = lines[start_idx:end_idx]

    # -------------------------
    # Cleanup: remove leftover UI / crypto / boilerplate
    # -------------------------
    kill_exact = {"×", "Post", "Share", "Email", "Send Email!", "Follow Us!", "copy"}
    kill_contains = [
        "Share this page",
        "Enter URL or ID to Unroll",
        "How to get URL link on X",
        "Copy Link to Tweet",
        "Paste it above and click",
        "More info at",
        "Twitter Help",
        "@threadreaderapp unroll",
        "Practice here",
        "Read on X",
        "Scrolly",
    ]

    cleaned = []
    for ln in core:
        if ln in kill_exact:
            continue
        if any(s.lower() in ln.lower() for s in kill_contains):
            continue
        # Strip obvious crypto addresses
        if re.match(r"^0x[a-fA-F0-9]{40}$", ln):  # Ethereum
            continue
        if re.match(r"^[13]{25,34}$", ln):  # Bitcoin
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
    
### START: DOT WORTHINESS SESSION STATE ###

if "worthiness_score" not in st.session_state:
    st.session_state.worthiness_score = None
if "worthiness_label" not in st.session_state:
    st.session_state.worthiness_label = ""
if "worthiness_meaning" not in st.session_state:
    st.session_state.worthiness_meaning = ""
if "worthiness_reasons" not in st.session_state:
    st.session_state.worthiness_reasons = []

### END: DOT WORTHINESS SESSION STATE ###

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
        
        # --- Compute Dot Worthiness (A: Skill OS compounding) ---
        score, reasons = dot_worthiness_score(st.session_state.extracted_text)
        label, meaning = dot_worthiness_label(score)
        
        st.session_state.worthiness_score = score
        st.session_state.worthiness_label = label
        st.session_state.worthiness_meaning = meaning
        st.session_state.worthiness_reasons = reasons
        
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
    st.subheader("Dot Worthiness")

    score = st.session_state.worthiness_score
    label = st.session_state.worthiness_label
    meaning = st.session_state.worthiness_meaning
    reasons = st.session_state.worthiness_reasons
    
    # Dial-like visualization (0–100) + label
    st.progress(int(score * 10))
    st.markdown(
        f"### **{score} / 10 — {label}**  \n"
        f"{meaning}",
        unsafe_allow_html=True
    )
    
    # 🔥 moment
    if score == 10.0:
        st.success("🔥🔥🔥 CANONICAL DOT ASSET 🔥🔥🔥")
        st.balloons()
    
    with st.expander("Why this score?", expanded=False):
        for r in reasons[:10]:
            st.write(f"- {r}")

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
