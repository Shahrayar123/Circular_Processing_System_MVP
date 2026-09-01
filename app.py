"""AuditPilot MVP — demonstration UI.

    streamlit run app.py

Four tabs that follow the pipeline: what came in, what was read, what was proposed, and
what comes out. Deliberately simple — no login, no roles, no approval workflow. Those
belong to the real build.
"""

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mvp import config, review, store  # noqa: E402

st.set_page_config(page_title="AuditPilot MVP", page_icon="📑", layout="wide")

COLOUR = {"New": "#2E7A4F", "Amendment": "#9C6F11", "Deletion": "#B03A30",
          "No action": "#64757A"}


# ====== HELPERS ======

# Bump when the schema changes — otherwise a cached frame from before the change is
# served and columns appear to be missing.
SCHEMA_VERSION = 3


@st.cache_data(show_spinner=False)
def load(sql: str, params: tuple = (), _v: int = SCHEMA_VERSION) -> pd.DataFrame:
    return pd.DataFrame(store.query(sql, params))


def chip(text: str, colour: str) -> str:
    return (f"<span style='background:{colour}1A;color:{colour};padding:2px 9px;"
            f"border-radius:3px;font-size:12px;font-weight:600'>{text}</span>")


def guide(purpose: str, points: list[tuple[str, str]]) -> None:
    """A short explanation of what a screen is for and how to read it."""
    st.markdown(f"<p style='color:#3D4B4D;font-size:14.5px;margin:2px 0 6px 0'>{purpose}</p>",
                unsafe_allow_html=True)
    with st.expander("How to read this screen"):
        for label, text in points:
            st.markdown(f"**{label}** — {text}")


def database_missing() -> bool:
    if not Path(config.DB_PATH).exists():
        st.error("No demo database yet.")
        st.code("python run_pipeline.py", language="bash")
        st.caption("Run that once, then reload this page.")
        return True
    return False


# ====== HEADER ======

st.markdown(
    "<h1 style='margin-bottom:2px'>Circular Processing System - MVP</h1>"
    "<p style='color:#64757A;margin-top:0;font-size:15px'>"
    "Preparation &amp; amendment of the audit checklist — working demonstration</p>",
    unsafe_allow_html=True,
)

if database_missing():
    st.stop()

counts = store.counts()
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["  Dashboard  ", "  Documents  ", "  Review  ", "  Approvals  ",
     "  Exports  ", "  Solution map  "])


# ====== 1 · OVERVIEW ======

with tab1:
    guide(
        "The weekly picture: what arrived, how much of it needed action, and what the "
        "system is proposing.",
        [
            ("The six figures across the top",
             "read them left to right — they follow the pipeline. Documents received, "
             "how many were duplicates, how many clauses came out of the rest, how many "
             "of those clauses actually create an obligation, and how many changes that "
             "produced. The drop from clauses to actionable is normal: most of a policy "
             "manual is background, not instructions."),
            ("Proposed changes",
             "the four outcomes. **New** means no existing test covers the obligation. "
             "**Amendment** means one does but the requirement has changed. "
             "**Deletion** means the clause withdraws or supersedes something. "
             "**No action** means nothing testable."),
            ("Where the documents came from",
             "the three intake routes. RIA emails are counted separately by body and "
             "attachment because content arrives either way."),
            ("Duplicates detected",
             "matched on the content of the file, not its name — so the same circular "
             "arriving twice, or by two different routes, is only worked once."),
        ])

    c = st.columns(7)
    c[0].metric("Audit tests indexed", f"{counts['tests']:,}")
    c[1].metric("Documents received", counts["documents"] + counts["duplicates"])
    c[2].metric("Duplicates skipped", counts["duplicates"])
    c[3].metric("Clauses extracted", counts["clauses"])
    c[4].metric("Actionable", counts["actionable"])
    c[5].metric("Changes proposed", counts["proposals"])
    c[6].metric("Approved for export", counts["approved"])

    st.caption(
        "Process flow — circular intake → AI analysis → Excel working file → "
        "human review → eAudit hand-off. Each tab above is one stage of it.")

    if counts.get("nested") or counts.get("failed") or counts.get("ocr_pages"):
        extra = st.columns(3)
        extra[0].metric("Found inside other documents", counts.get("nested", 0))
        extra[1].metric("Pages read by OCR", counts.get("ocr_pages", 0))
        extra[2].metric("Could not be read", counts.get("failed", 0))

    st.divider()
    left, right = st.columns([1, 1])

    with left:
        st.subheader("Proposed changes")
        by_type = load("SELECT change_type, COUNT(*) n FROM proposals "
                       "GROUP BY change_type ORDER BY n DESC")
        if not by_type.empty:
            st.bar_chart(by_type.set_index("change_type"), color="#0E6E6E", height=240)
            for _, row in by_type.iterrows():
                st.markdown(f"{chip(row['change_type'], COLOUR.get(row['change_type'], '#555'))}"
                            f" &nbsp; {row['n']}", unsafe_allow_html=True)

    with right:
        st.subheader("Where the documents came from")
        by_source = load(
            "SELECT CASE source WHEN 'folder' THEN 'Circular directory' "
            "WHEN 'email-body' THEN 'RIA email — body' "
            "WHEN 'email-attachment' THEN 'RIA email — attachment' ELSE source END AS route, "
            "COUNT(*) n FROM documents WHERE status != 'duplicate' GROUP BY source")
        st.dataframe(by_source, hide_index=True, use_container_width=True)
        st.caption(
            "ABL confirmed RIA emails carry the content either in the body or as an "
            "attachment. Both routes are handled, and each document records which one "
            "it arrived by.")

    st.divider()
    fcol, dcol = st.columns([1, 1])

    with fcol:
        st.subheader("Formats received")
        formats = load(
            "SELECT COALESCE(kind, 'unknown') AS Format, COUNT(*) AS n FROM documents "
            "WHERE status != 'duplicate' GROUP BY kind ORDER BY n DESC")
        st.dataframe(formats, hide_index=True, use_container_width=True)
        st.caption("Circulars arrive as scanned PDFs, images, native PDFs, Word, Excel, "
                   "Word containing Excel and legacy formats. A document found inside "
                   "another is recorded against its parent.")

    with dcol:
        st.subheader("Could not be read")
        broken = load("SELECT filename AS File, error AS Reason FROM documents "
                      "WHERE status = 'error'")
        if broken.empty:
            st.caption("None — every document was read.")
        else:
            st.dataframe(broken, hide_index=True, use_container_width=True)
        st.caption("A file that cannot be read is recorded with its reason, never "
                   "dropped. A circular that disappears quietly is the worst outcome — "
                   "the team believes it was processed.")

    st.divider()
    st.subheader("Duplicates detected")
    dupes = load(
        "SELECT d.filename AS 'Skipped file', o.filename AS 'Same content as', d.source AS Source "
        "FROM documents d JOIN documents o ON o.id = d.duplicate_of WHERE d.status = 'duplicate'")
    if dupes.empty:
        st.caption("None.")
    else:
        st.dataframe(dupes, hide_index=True, use_container_width=True)
        st.caption("Matched on SHA-256 of the file content, so the same circular arriving "
                   "twice — or by two different routes — is only processed once.")


# ====== 2 · DOCUMENTS ======

with tab2:
    guide(
        "What the system read in each document, and which parts it judged to need audit "
        "work. This is the screen that shows the reading is grounded in the source.",
        [
            ("Pick a document", "the list holds everything ingested; duplicates are "
                                "excluded because they were never processed."),
            ("Each clause block", "one clause of the document. The green tag means it "
                                  "creates an obligation and goes forward to matching; "
                                  "the grey tag means for information only."),
            ("page N · char X–Y", "exactly where the clause sits in the source. Every "
                                  "later proposal can be traced back to this position — "
                                  "that is what makes the output auditable."),
            ("The grey line underneath", "why the clause was judged actionable or not."),
            ("The toggle", "off by default so you see only the clauses that mattered. "
                           "Turn it on to see everything that was set aside."),
        ])

    docs = load("SELECT * FROM documents WHERE status = 'ingested' ORDER BY id")
    if docs.empty:
        st.info("Nothing ingested yet.")
    else:
        labels = {int(r["id"]): f"{r['filename'][:58]}" for _, r in docs.iterrows()}
        chosen = st.selectbox("Document", list(labels), format_func=lambda i: labels[i])
        doc = docs[docs["id"] == chosen].iloc[0]

        meta = st.columns(5)
        meta[0].markdown(f"**Title**  \n{doc['title'] or '—'}")
        meta[1].markdown(f"**Received via**  \n{doc['source']}")
        meta[2].markdown(f"**Format**  \n{doc.get('kind') or '—'}"
                         + (f"  \n_{int(doc['ocr_pages'])} page(s) OCR'd_"
                            if doc.get("ocr_pages") else ""))
        meta[3].markdown(f"**Pages**  \n{doc['pages'] or '—'}")
        meta[4].markdown(f"**Date found**  \n{doc['doc_date'] or '—'}")

        if doc.get("parent_id"):
            parent = store.one("SELECT filename FROM documents WHERE id = ?",
                               (int(doc["parent_id"]),))
            st.info(f"This document was found **inside** "
                    f"`{(parent or {}).get('filename', 'another document')}`.", icon="📎")
        kids = load("SELECT filename FROM documents WHERE parent_id = ?",
                    (int(doc["id"]),))
        if not kids.empty:
            st.info("Documents found inside this one: "
                    + ", ".join(f"`{n}`" for n in kids["filename"]), icon="📎")
        if doc["source_detail"]:
            st.caption(f"Source: {doc['source_detail']}")

        clauses = load(
            "SELECT sequence, clause_ref, is_actionable, strata_tag, reason, text, "
            "page_number, char_start, char_end FROM clauses WHERE document_id = ? "
            "ORDER BY sequence", (int(chosen),))

        actionable = int(clauses["is_actionable"].sum()) if not clauses.empty else 0
        st.markdown(f"**{len(clauses)} clauses extracted · {actionable} actionable**")

        show_all = st.toggle("Show clauses marked 'for information only'", value=False)
        for _, cl in clauses.iterrows():
            if not cl["is_actionable"] and not show_all:
                continue
            tag = ("Actionable" if cl["is_actionable"] else "For information only")
            colour = "#0E6E6E" if cl["is_actionable"] else "#64757A"
            st.markdown(
                f"{chip(tag, colour)} &nbsp; **{cl['clause_ref']}** &nbsp; "
                f"<span style='color:#64757A;font-size:12px'>page {cl['page_number']} · "
                f"char {cl['char_start']}–{cl['char_end']}"
                f"{' · ' + cl['strata_tag'] if cl['strata_tag'] else ''}</span>",
                unsafe_allow_html=True)
            st.markdown(
                f"<div style='border-left:3px solid {colour};padding:6px 0 6px 12px;"
                f"margin:4px 0 14px 0;font-size:14px'>{cl['text'][:900]}"
                f"<div style='color:#64757A;font-size:12px;margin-top:6px'>{cl['reason']}</div>"
                f"</div>", unsafe_allow_html=True)


# ====== 3 · PROPOSALS ======

with tab3:
    guide(
        "Level 1 review. Every proposed change is checked against the clause that "
        "produced it, then approved, rejected or sent back. Nothing reaches eAudit "
        "without passing this screen and then Approvals.",
        [
            ("This is a work queue", "it shows what is **still waiting for you**. Once "
                                     "you approve, reject or send back a proposal it "
                                     "leaves the queue and the next one comes up. "
                                     "Switch to *All proposals* to see everything again, "
                                     "including what you have already decided."),
            ("The grid", "one row per proposed change, the same rows that go into the "
                         "Excel working file. Filter by change type or department, or "
                         "search the clause wording."),
            ("Sr #", "ABL's identifier shape — year, week, sequence and strata code."),
            ("Existing Test Code", "filled only when an existing test is being amended "
                                   "or deleted. Blank for a new test."),
            ("One at a time, in order", "the screen shows a single proposal, following "
                                        "the same clause order as the Documents tab. "
                                        "Decide it and the next one appears — there is "
                                        "no list to choose from, because the job is to "
                                        "work through them."),
            ("The review card", "the clause on the left, what is proposed on the right. "
                                "Read them together — the proposal should be justified "
                                "by the clause beside it and nothing else."),
            ("Skip for now", "moves past a proposal without deciding it. It stays in the "
                             "queue and comes back once the rest are done."),
            ("Candidate tests that were considered",
             "the shortlist the search returned before the decision was made. This is "
             "the evidence: it shows what the system looked at, not just what it chose. "
             "Every code listed exists in the library — a code that cannot be validated "
             "is dropped before it reaches this screen."),
        ])

    # Ordered by document then clause sequence — the same order the Documents tab shows,
    # so a reviewer works through a circular top to bottom rather than jumping about.
    proposals = load(
        "SELECT p.*, c.text AS clause_text, c.clause_ref, c.page_number, c.sequence, "
        "d.filename, d.title AS doc_title FROM proposals p "
        "JOIN clauses c ON c.id = p.clause_id "
        "JOIN documents d ON d.id = p.document_id "
        "ORDER BY p.document_id, c.sequence")

    if proposals.empty:
        st.info("No proposals yet.")
    else:
        # A reviewer works a queue: once a proposal is decided it should leave, and the
        # next one should come up. Showing every proposal regardless of status makes it
        # look as though an approval did nothing.
        AWAITING = (review.PENDING_L1, review.CHANGES)
        pending = proposals[proposals["status"].isin(AWAITING)]
        decided = len(proposals) - len(pending)

        f0, f1, f2, f3 = st.columns([1.3, 1, 1, 1.8])
        mode = f0.radio("Show", ["Awaiting my review", "All proposals"],
                        index=0, key="review_mode")
        types = f1.multiselect("Change type", sorted(proposals["change_type"].unique()),
                               default=sorted(proposals["change_type"].unique()))
        depts = f2.multiselect("Department", sorted(proposals["department"].unique()),
                               default=sorted(proposals["department"].unique()))
        search = f3.text_input("Search the clause text", "")

        base = pending if mode == "Awaiting my review" else proposals
        view = base[base["change_type"].isin(types) & base["department"].isin(depts)]
        if search:
            view = view[view["clause_text"].str.contains(search, case=False, na=False)]

        # NOTE: never st.stop() here — it halts the whole page and the other tabs
        # disappear. Guard the rest of the screen instead.
        queue_empty = view.empty and mode == "Awaiting my review"
        if queue_empty:
            st.success("Level 1 review complete — nothing left in the queue.", icon="✅")
            st.caption("Proposals you approved have moved to the **Approvals** tab for "
                       "level 2 sign-off. Switch to *All proposals* above to see what "
                       "you decided.")

        st.caption(f"{len(view)} shown"
                   + (f" · {len(proposals)} proposals in total" if mode == "All proposals"
                      else f" · awaiting your review"))

    if not proposals.empty and not view.empty:
        grid = view[["sr_no", "change_type", "department", "strata", "target_test_code",
                     "proposed_test_description", "risk_rating", "confidence"]].fillna("")
        st.dataframe(
            grid.rename(columns={
                      "sr_no": "Sr #", "change_type": "Test Type", "department": "Dep",
                      "strata": "Strata", "target_test_code": "Existing Test Code",
                      "proposed_test_description": "New Test / Change",
                "risk_rating": "Risk", "confidence": "Conf."}),
            hide_index=True, use_container_width=True, height=280)

        st.divider()
        # One proposal at a time, in document order. No dropdown: the reviewer's job is
        # to work through them, not to choose where to start. Deciding removes the item
        # from the queue, so the next one appears on the rerun.
        skipped = st.session_state.setdefault("skipped", set())
        remaining = [i for i in view["id"] if i not in skipped]
        if not remaining:                      # everything left was skipped — start over
            skipped.clear()
            remaining = list(view["id"])

        pick = remaining[0]
        p = view[view["id"] == pick].iloc[0]

        # Count through the WHOLE run, not the queue. `pick` is always the first item
        # left, so an index into the queue is permanently 1 and the bar never moves.
        total = len(proposals)
        st.subheader(f"Reviewing {decided + 1} of {total}")
        st.caption(
            f"{p['filename']}  ·  clause **{p['clause_ref']}**  ·  page {p['page_number']}"
            + f"  ·  {len(view)} left in the queue"
            + (f"  ·  {len(skipped)} skipped" if skipped else ""))
        st.progress(decided / total if total else 0.0,
                    text=f"{decided} of {total} reviewed")

        left, right = st.columns(2)
        with left:
            st.markdown("**The clause**")
            st.markdown(
                f"<div style='border-left:3px solid #0E6E6E;padding:8px 0 8px 12px;"
                f"font-size:14px'>{p['clause_text'][:1400]}</div>", unsafe_allow_html=True)

        with right:
            st.markdown(
                f"**Proposal** &nbsp; {chip(p['change_type'], COLOUR.get(p['change_type'], '#555'))}",
                unsafe_allow_html=True)
            rows = {
                "Sr #": p["sr_no"], "Department": p["department"], "Strata": p["strata"],
                "Amendment type": p["amendment_type"] or "—",
                "Existing test": p["target_test_code"] or "—",
                "Risk rating": p["risk_rating"], "Root cause": p["root_cause"],
                "Confidence": f"{p['confidence']:.2f}",
            }
            st.dataframe(pd.DataFrame(rows.items(), columns=["Field", "Value"]),
                         hide_index=True, use_container_width=True)

            if p["existing_test_description"]:
                st.markdown("**Existing test**")
                st.info(p["existing_test_description"])
            if p["proposed_test_description"]:
                st.markdown("**Proposed test**")
                st.success(p["proposed_test_description"])
            st.markdown("**Rationale**")
            st.caption(p["rationale"])

        st.markdown("**Candidate tests that were considered**")
        candidates = json.loads(p["candidates"] or "[]")
        if candidates:
            st.dataframe(
                pd.DataFrame(candidates).rename(columns={
                    "test_code": "Test code", "test_description": "Description",
                    "strata": "Strata", "score": "Score"}),
                hide_index=True, use_container_width=True)
            st.caption("Retrieved from the 500-test library by keyword and vector search, "
                       "then fused. Every code shown exists in the library — codes that "
                       "cannot be validated are dropped before a reviewer sees them.")

        st.divider()
        status = p.get("status", review.PENDING_L1) or review.PENDING_L1
        st.markdown(f"**Level 1 decision** &nbsp; "
                    f"{chip(status, '#0E6E6E' if status == 'Approved' else '#9C6F11')}",
                    unsafe_allow_html=True)
        note = st.text_input("Reviewer note (optional)", key=f"note{pick}")
        b1, b2, b3, b4, _ = st.columns([1, 1, 1.4, 1.2, 2])
        if b1.button("Approve", key=f"a{pick}", type="primary"):
            new_status = review.decide(int(pick), 1, review.APPROVED, note)
            st.cache_data.clear()
            st.success(f"Level 1 approved — now {new_status}. It moves to the Approvals tab.")
            st.rerun()
        if b2.button("Reject", key=f"r{pick}"):
            review.decide(int(pick), 1, review.REJECTED, note)
            st.cache_data.clear()
            st.warning("Rejected — it will not be exported.")
            st.rerun()
        if b3.button("Request changes", key=f"c{pick}"):
            review.decide(int(pick), 1, review.CHANGES, note)
            st.cache_data.clear()
            st.info("Sent back for changes.")
            st.rerun()
        if b4.button("Skip for now", key=f"s{pick}"):
            # No decision recorded — it stays in the queue and comes back at the end.
            skipped.add(pick)
            st.rerun()

        past = review.history(int(pick))
        if past:
            st.caption("Decision history")
            st.dataframe(pd.DataFrame(past).rename(columns={
                "level": "Level", "decision": "Decision", "note": "Note",
                "decided_at": "When"}), hide_index=True, use_container_width=True)

        st.caption(
            "Level 1 approval **advances** the proposal — it does not finalise it. "
            "Final sign-off happens on the Approvals tab, matching the two-level "
            "hierarchy in the agreed solution.")


# ====== 4 · APPROVALS (level 2) ======

with tab4:
    guide(
        "Level 2 sign-off — the audit head. This is the last step before anything can be "
        "exported to eAudit.",
        [
            ("What appears here", "only proposals that passed level 1. A proposal still "
                                  "awaiting review, rejected, or sent back for changes "
                                  "does not reach this queue."),
            ("The table", "every proposal waiting for you, in clause order. The last two "
                          "columns show what the reviewer decided — their note and when "
                          "they signed it off — so you can see level 1 was done, not "
                          "assumed."),
            ("One at a time", "below the table, a single proposal in the same clause "
                              "order. Sign it off and the next appears. Skip for now "
                              "moves past without deciding."),
            ("Approve", "marks the change final and makes it eligible for the eAudit "
                        "export. Nothing else does."),
            ("Deletions", "always confirm the affected test before approving — a "
                          "withdrawn test cannot be recovered from the export."),
        ])

    # Same order as Review and the Documents tab — document, then clause sequence.
    # reviewed_at shows when level 1 signed off, so the approver can see the reviewer
    # acted rather than taking it on trust.
    queue = load(
        "SELECT p.*, c.text AS clause_text, c.clause_ref, c.page_number, c.sequence, "
        "d.filename, "
        "(SELECT MAX(a.decided_at) FROM approvals a "
        " WHERE a.proposal_id = p.id AND a.level = 1) AS reviewed_at "
        "FROM proposals p "
        "JOIN clauses c ON c.id = p.clause_id JOIN documents d ON d.id = p.document_id "
        "WHERE p.status = ? ORDER BY p.document_id, c.sequence", (review.PENDING_L2,))

    approved_now = load("SELECT sr_no, change_type, target_test_code, department, "
                        "proposed_test_description FROM proposals WHERE status = ? "
                        "ORDER BY id", (review.APPROVED,))

    m = st.columns(4)
    m[0].metric("Awaiting level 2", len(queue))
    m[1].metric("Approved", len(approved_now))
    m[2].metric("Rejected", int(store.scalar(
        "SELECT COUNT(*) FROM proposals WHERE status = 'Rejected'") or 0))
    m[3].metric("Still at level 1", counts["proposals"] - len(queue) - len(approved_now)
                - int(store.scalar("SELECT COUNT(*) FROM proposals WHERE status = 'Rejected'") or 0))

    st.divider()
    if queue.empty:
        st.info("Nothing awaiting level 2. Approve something on the Review tab first.")
    else:
        st.markdown(f"**{len(queue)} proposal(s) awaiting final sign-off**")
        table = queue[["sr_no", "clause_ref", "page_number", "change_type", "department",
                       "target_test_code", "proposed_test_description",
                       "reviewer_note", "reviewed_at"]].fillna("")
        table["reviewed_at"] = table["reviewed_at"].astype(str).str.replace("T", " ")
        st.dataframe(
            table.rename(columns={
                "sr_no": "Sr #", "clause_ref": "Clause", "page_number": "Page",
                "change_type": "Test Type", "department": "Dep",
                "target_test_code": "Existing Test Code",
                "proposed_test_description": "New Test / Change",
                "reviewer_note": "Reviewer note",
                "reviewed_at": "Approved at L1"}),
            hide_index=True, use_container_width=True, height=220)
        st.caption("Every row here was approved by the reviewer at level 1 — the last two "
                   "columns show their note and when they signed it off.")

        # One at a time, in clause order. No dropdown: sign-off is a queue, not a menu.
        skipped2 = st.session_state.setdefault("skipped_l2", set())
        remaining2 = [i for i in queue["id"] if i not in skipped2]
        if not remaining2:
            skipped2.clear()
            remaining2 = list(queue["id"])

        pick2 = remaining2[0]
        q = queue[queue["id"] == pick2].iloc[0]

        st.divider()
        signed = int(store.scalar(
            "SELECT COUNT(DISTINCT proposal_id) FROM approvals WHERE level = 2") or 0)
        st.subheader(f"Signing off {signed + 1} of {signed + len(queue)}")
        st.caption(
            f"{q['filename']}  ·  clause **{q['clause_ref']}**  ·  page {q['page_number']}"
            + f"  ·  {len(queue)} left in this queue"
            + (f"  ·  {len(skipped2)} skipped" if skipped2 else ""))
        st.progress(signed / max(signed + len(queue), 1),
                    text=f"{signed} of {signed + len(queue)} signed off")

        col1, col2 = st.columns(2)
        col1.markdown("**Source clause**")
        col1.markdown(f"<div style='border-left:3px solid #0E6E6E;padding:8px 0 8px 12px;"
                      f"font-size:14px'>{q['clause_text'][:900]}</div>",
                      unsafe_allow_html=True)
        col2.markdown(f"**Proposed** &nbsp; "
                      f"{chip(q['change_type'], COLOUR.get(q['change_type'], '#555'))}",
                      unsafe_allow_html=True)
        if q["proposed_test_description"]:
            col2.success(q["proposed_test_description"])
        if q["existing_test_description"]:
            col2.info(f"Existing {q['target_test_code']}: {q['existing_test_description']}")
        col2.caption(q["rationale"])
        when = str(q["reviewed_at"]).replace("T", " ") if q["reviewed_at"] else ""
        note_line = ("<br>Reviewer note: " + str(q["reviewer_note"])
                     if q["reviewer_note"] else "")
        col2.markdown(
            "<div style='background:#0E6E6E14;border-left:3px solid #0E6E6E;"
            "padding:8px 12px;margin-top:8px;font-size:13px'>"
            "<b>Approved by the reviewer at level 1</b>"
            + ("  ·  " + when if when else "") + note_line + "</div>",
            unsafe_allow_html=True)

        if q["change_type"] == "Deletion":
            st.warning("This is a **deletion**. Confirm the affected test before approving.",
                       icon="⚠️")

        note2 = st.text_input("Approver note (optional)", key=f"n2{pick2}")
        d1, d2, d3, d4, _ = st.columns([1.4, 1, 1.6, 1.2, 2])
        if d1.button("Approve (final)", key=f"a2{pick2}", type="primary"):
            review.decide(int(pick2), 2, review.APPROVED, note2)
            st.cache_data.clear()
            st.success("Approved. It is now eligible for the eAudit export.")
            st.rerun()
        if d2.button("Reject", key=f"r2{pick2}"):
            review.decide(int(pick2), 2, review.REJECTED, note2)
            st.cache_data.clear()
            st.rerun()
        if d3.button("Send back to level 1", key=f"c2{pick2}"):
            review.decide(int(pick2), 2, review.CHANGES, note2)
            st.cache_data.clear()
            st.rerun()
        if d4.button("Skip for now", key=f"s2{pick2}"):
            skipped2.add(pick2)
            st.rerun()

    if not approved_now.empty:
        st.divider()
        st.markdown(f"**Approved — {len(approved_now)} ready for eAudit**")
        st.dataframe(approved_now.fillna("").rename(columns={
            "sr_no": "Sr #", "change_type": "Test Type", "department": "Dep",
            "target_test_code": "Existing Test Code",
            "proposed_test_description": "New Test / Change"}),
            hide_index=True, use_container_width=True)

    st.divider()
    if st.button("Reset all review decisions"):
        review.reset_all()
        st.cache_data.clear()
        st.rerun()
    st.caption("Resets the demo so the review flow can be walked through again.")


# ====== 5 · EXPORTS ======

with tab5:
    guide(
        "Two different things live on this screen. The **working files** are what the "
        "audit team reviews from — they contain every proposal, approved or not. The "
        "**eAudit hand-off** is the opposite: approved changes only.",
        [
            ("Working files come BEFORE review",
             "the Excel and Word files are the reviewer's input. They deliberately "
             "contain proposals that have not been approved — that is the point of them. "
             "Every row in the Excel carries a **Review Status** column so its position "
             "is never in doubt."),
            ("The eAudit hand-off comes AFTER approval",
             "this is the only file gated on approval. A proposal reaches it only after "
             "level 1 **and** level 2 have approved it. Approve nothing and there is "
             "nothing to generate."),
            ("Why the distinction matters",
             "the working file existing does not mean anything has been approved. The "
             "eAudit file existing does."),
        ])

    st.subheader("Working files — for review, all proposals included")
    files = sorted(config.OUTPUT_DIR.glob("*")) if config.OUTPUT_DIR.exists() else []
    if not files:
        st.info("Nothing generated yet — run `python run_pipeline.py`.")
    else:
        excel = [f for f in files if f.suffix == ".xlsx"]
        words = [f for f in files if f.suffix == ".docx"]

        if excel:
            st.markdown("**Excel working file** — Summary · Proposed Tests · Week MIS")
            st.caption(f"Contains all {counts['proposals']} proposals, each showing its "
                       f"review status. {counts['approved']} approved so far.")
            for f in excel:
                st.download_button(f"⬇  {f.name}", f.read_bytes(), file_name=f.name,
                                   mime="application/vnd.openxmlformats-officedocument."
                                        "spreadsheetml.sheet", key=f"x{f.name}")
            st.caption("Additions in green text, deletions in red strike-through, per the "
                       "BRD formatting rules. The live system writes all six sheets and "
                       "78 columns of ABL's working file.")

        st.divider()
        st.markdown(f"**Word working documents** — one per circular ({len(words)})")
        for f in words:
            st.download_button(f"⬇  {f.name}", f.read_bytes(), file_name=f.name,
                               mime="application/vnd.openxmlformats-officedocument."
                                    "wordprocessingml.document", key=f"w{f.name}")
        st.caption("Each clause carries a working note in the audit team's own convention "
                   "— \"New test …\", \"Covered in …\", or \"Information\".")

    st.divider()
    st.subheader("eAudit hand-off — approved changes only")
    approved_n = counts["approved"]
    if approved_n == 0:
        st.error(
            f"**Nothing can be exported.** {counts['proposals']} proposals exist and "
            f"**0 are approved**, so there is nothing eligible for eAudit. Approve at "
            f"level 1 on the Review tab, then at level 2 on Approvals, and this button "
            f"will appear.", icon="🔒")
    else:
        if st.button(f"Generate eAudit export  ({approved_n} approved)", type="primary"):
            path, refusals = review.export_eaudit()
            st.cache_data.clear()
            if path:
                st.success(f"Written: {path.name}")
            if refusals:
                st.warning(f"{len(refusals)} proposal(s) were **excluded** because they "
                           f"are not fully approved:")
                st.code(chr(10).join(refusals[:15]), language="text")

    eaudit = config.OUTPUT_DIR / "eAudit_BAC_Export.xlsx"
    if eaudit.exists():
        st.download_button(f"⬇  {eaudit.name}", eaudit.read_bytes(), file_name=eaudit.name,
                           mime="application/vnd.openxmlformats-officedocument."
                                "spreadsheetml.sheet", key="eaudit")
    st.caption(
        "Only proposals approved at **both** levels appear in this file, and anything "
        "unapproved is named rather than silently dropped. In the delivered system the "
        "export refuses to run at all while unapproved items are in scope.")

    st.divider()
    with st.expander("What the MVP leaves out"):
        st.markdown("""
| Live system | This demo |
|---|---|
| PostgreSQL 18 + pgvector | one SQLite file |
| BGE embeddings, 1024-d | TF-IDF vectors in numpy — no model download |
| 7,500 real audit tests | 500 generated sample tests |
| Qwen2.5-VL OCR, Tesseract fallback | not needed — every supplied file has a text layer |
| 72B reasoning model on the H100 | deterministic rules (or a local model, optionally) |
| Six sheets, 78 columns | three sheets, the columns that tell the story |
| Reviewer → Approver workflow | **present** — two levels, with a decision history |
| Named users, roles, RBAC, append-only audit log | not included |
| Quarterly eAudit export | **present** — approved changes only |
| Versioning and snapshots on every edit | not included |
""")


# ====== 6 · SOLUTION MAP ======

with tab6:
    guide(
        "How this demonstration lines up with the agreed functional document — which "
        "parts are the proposed solution working, and which are simplified for the demo.",
        [
            ("Why this screen exists", "so there is no doubt that the MVP is the "
                                       "proposed system in miniature rather than "
                                       "something different."),
            ("Read the right-hand column", "it says what the MVP does for each proposed "
                                           "component, and where a shortcut was taken."),
        ])

    st.subheader("Process flow")
    st.markdown(
        "<div style='font-family:monospace;font-size:13.5px;line-height:2;color:#3D4B4D'>"
        "circular intake &nbsp;→&nbsp; AI analysis &nbsp;→&nbsp; Excel working file "
        "&nbsp;→&nbsp; human review &nbsp;→&nbsp; eAudit hand-off</div>",
        unsafe_allow_html=True)
    st.caption("Figure 2 of the functional document. Every stage is present in this demo.")

    st.divider()
    st.subheader("Components")
    st.dataframe(pd.DataFrame([
        {"Proposed component": "Ingestion Service",
         "In this demo": "Circular directory + RIA mailbox (body and attachment), "
                         "attachment extraction, SHA-256 de-duplication",
         "Simplified": "Files rather than a live IMAP connection"},
        {"Proposed component": "Document Processing",
         "In this demo": "PDF / DOCX / email reading, clause splitting, actionable "
                         "tagging with strata",
         "Simplified": "No OCR — every supplied document has a text layer"},
        {"Proposed component": "Retrieval & Matching",
         "In this demo": "BM25 + vector search over the test library, fused, top-K "
                         "candidates per clause",
         "Simplified": "TF-IDF vectors instead of BGE; 500 tests instead of 7,500"},
        {"Proposed component": "AI Decision Engine",
         "In this demo": "New / Amendment / Deletion / No action, with proposed wording, "
                         "rationale and validated test codes",
         "Simplified": "Rules instead of the 72B model (optionally a local model)"},
        {"Proposed component": "Review Application",
         "In this demo": "Two-level review — level 1 reviewer, level 2 approver, with a "
                         "decision history per proposal",
         "Simplified": "No login, roles or append-only audit log"},
        {"Proposed component": "Output & Integration",
         "In this demo": "Word working document per circular; Excel working file; eAudit "
                         "export of approved changes only",
         "Simplified": "Three sheets rather than six; notes in a column rather than "
                       "Word comments"},
    ]), hide_index=True, use_container_width=True)

    st.divider()
    st.subheader("Rules kept exactly as proposed")
    for line in [
        "**Nothing is exported without human approval.** Two levels, and level 1 only "
        "advances a proposal — it never finalises it.",
        "**Every cited test code is validated against the library** before a reviewer "
        "sees it. A code that cannot be validated is dropped, not displayed.",
        "**Deletions are flagged for explicit confirmation** and are never auto-approved.",
        "**Every proposal traces back to its source clause**, with the page and character "
        "position it came from.",
        "**Excel is an output, not the database.** Editing an exported file changes "
        "nothing in the system.",
    ]:
        st.markdown(f"- {line}")
