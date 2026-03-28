import re
import json
import datetime
import streamlit as st

st.set_page_config(page_title="Dot Tweet/Thread Ingestor", layout="wide")

st.title("Dot Tweet/Thread Ingestor (v0)")
st.caption("Paste a tweet or thread. Export Dot-ready artifacts.")

with st.sidebar:
    st.header("Metadata")
    url = st.text_input("Tweet/Thread URL (optional)")
    author = st.text_input("Author handle/name (optional)")
    published = st.text_input("Published date/time (optional)")
    ingested_at = datetime.date.today().isoformat()
    intent = st.selectbox(
        "Intent shelf",
        ["Thesis-Building","Mental R&D","Decision Support","Capability Building","Challenge","Context"]
    )
    state = st.selectbox("Source state", ["Unread","In-Progress","Ingested","Archived"])
    confidence = st.selectbox("Confidence", ["Provisional","Validated"])
    title = st.text_input("Title", value="Tweet/Thread")

text = st.text_area("Paste tweet/thread text", height=300)

def normalize(t):
    t = t.replace("\r\n","\n").strip()
    return re.sub(r"\n{3,}", "\n\n", t)

if st.button("Generate outputs"):
    if not text.strip():
        st.error("Please paste tweet text.")
        st.stop()

    body = normalize(text)

    source_md = f"""# S_TWEET: {title}

URL: {url or 'n/a'}
Author: {author or 'unknown'}
State: {state}
Intent: {intent}
Confidence: {confidence}
Ingested_at: {ingested_at}

---

{body}
"""

    payload = {
        "source": {
            "title": title,
            "url": url,
            "author": author,
            "state": state,
            "intent": intent,
            "confidence": confidence,
            "content": body
        }
    }

    prompt = f"""Dot: Librarian Ingest.

Input: {url or '[paste source link]'}
State: {state}
Intent shelf: {intent}
Confidence: {confidence}

Captured text:
---
{body}
"""

    st.download_button("Download source.md", source_md, file_name="source.md")
    st.download_button("Download dot_payload.json", json.dumps(payload,indent=2), file_name="dot_payload.json")
    st.text_area("Ingestion Prompt (copy/paste)", prompt, height=200)
