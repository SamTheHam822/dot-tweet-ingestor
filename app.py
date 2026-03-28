import re
import json
import datetime
import streamlit as st

st.set_page_config(page_title="Dot Tweet/Thread Ingestor", layout="wide")

st.title("Dot Tweet / Thread Ingestor (v0)")
st.caption("Paste a tweet or thread. Export Dot-ready artifacts: Source card + JSON payload + ingestion prompt.")

# -----------------------------
# Helpers
# -----------------------------
def normalize(t: str) -> str:
    t = t.replace("\r\n", "\n").strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t

def strip_recursive_prompt(t: str) -> str:
    """
    If user accidentally pastes a 'Dot: Librarian Ingest' prompt into the text box,
    strip that header so the captured text is only the tweet/thread content.
    """
    t = t.strip()

    # Remove a leading ingestion prompt block if present
    # Matches:
    # Dot: Librarian Ingest.
    # Input: ...
    # State: ...
    # Intent shelf: ...
    # Confidence: ...
    # Captured text:
    # ---
    pattern = r"(?is)^\s*Dot:\s*Librarian Ingest\.\s*.*?Captured text:\s*---\s*"
    t = re.sub(pattern, "", t)

    # If user pasted the prompt twice (as in your test), strip again
    t = re.sub(pattern, "", t)

    return t.strip()

def build_source_md(source_id, title, published, url, author, state, intent, confidence, ingested_at, body):
    return "\n".join([
        f"# {source_id}: {title} (Thread, Publish {published or 'unknown'})",
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
        "- Treat claims as **Provisional** unless verified against primary source text."
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
        "Notes: Create/Update Source node. Extract Concepts (C#) and Claims (K#) ONLY grounded in the pasted text. Merge duplicates; enforce naming conventions.",
        "",
        "Captured text:",
        "---",
        body
    ])

# -----------------------------
# Session state (persistent outputs)
# -----------------------------
if "generated" not in st.session_state:
    st.session_state.generated = False
if "source_md" not in st.session_state:
    st.session_state.source_md = ""
if "payload_str" not in st.session_state:
    st.session_state.payload_str = ""
if "prompt" not in st.session_state:
    st.session_state.prompt = ""

# -----------------------------
# UI: Main page inputs (mobile-friendly)
# -----------------------------
st.subheader("Source URL (optional)")
url_main = st.text_input(
    "Paste the tweet / thread URL here",
    placeholder="https://x.com/..."
)

st.subheader("Tweet / Thread text (paste)")
text = st.text_area(
    "Paste tweet/thread text here (thread OK)",
    height=280,
    placeholder="Paste text from X (or ThreadReader) here..."
)

# -----------------------------
# Sidebar metadata (optional)
# -----------------------------
with st.sidebar:
    st.header("Metadata (optional)")
    author = st.text_input("Author handle/name")
    published = st.text_input("Published date/time (freeform)")
    ingested_at = datetime.date.today().isoformat()

    intent = st.selectbox(
        "Intent shelf",
        ["Thesis-Building", "Mental R&D", "Decision Support", "Capability Building", "Challenge", "Context"],
        index=0
    )
    state = st.selectbox("Source state", ["Unread", "In-Progress", "Ingested", "Archived"], index=0)
    confidence = st.selectbox("Confidence", ["Provisional", "Validated"], index=0)
    title = st.text_input("Title", value="Tweet/Thread")

    strip_prompt = st.checkbox("Auto-remove pasted Dot prompt (recommended)", value=True)

# -----------------------------
# Generate outputs
# -----------------------------
if st.button("Generate outputs", type="primary"):
    if not text.strip():
        st.error("Please paste tweet/thread text.")
        st.stop()

    body = normalize(text)
    if strip_prompt:
        body = strip_recursive_prompt(body)

    source_id = "S_TWEET"
    url_final = url_main.strip() or None

    source_md = build_source_md(
        source_id=source_id,
        title=title,
        published=published,
        url=url_final,
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
            "url": url_final,
            "author": author,
            "published": published or "unknown",
            "ingested_at": ingested_at,
            "state": state,
            "intent": intent,
            "confidence": confidence,
            "content": body,
        },
        "nodes": [
            {"id": source_id, "type": "S", "name": f"{title} (Thread, Publish {published or 'unknown'})"}
        ],
        "edges": [],
        "placeholders": {"concepts": [], "claims": [], "frameworks": [], "work_artifacts": []}
    }

    prompt = build_prompt(url_final, state, intent, confidence, body)

    # Store persistently so downloads don't wipe the UI
    st.session_state.source_md = source_md
    st.session_state.payload_str = json.dumps(payload, indent=2)
    st.session_state.prompt = prompt
    st.session_state.generated = True

# -----------------------------
# Outputs (persist across downloads)
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

    st.info("Tip: On iPhone, long-press in the code block → Select All → Copy.")

st.markdown("---")
st.caption("v0 is paste-first. For URL-only ingestion, add an X API connector later with proper auth.")
