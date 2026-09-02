"""AuditPilot POC — the simple screen.

    streamlit run simple_app.py

ONE screen, three steps, in the order the audit team actually works:

    proposed changes  ->  reviewer signs off  ->  approver signs off  ->  Excel export

`app.py` is the full demonstration — six tabs, dashboards, document tracing, the
solution map. This file is deliberately the opposite: a table, two sign-off steps and a
download button. It is for the meeting where the point is the WORKFLOW, not the system.

It shares the same database, the same review rules and the same export as `app.py` —
nothing here is a mock-up. Approve a row on this screen and `app.py` shows it approved.
"""

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mvp import config, review, store  # noqa: E402

st.set_page_config(page_title="Circular Processing System",
                   page_icon="✅", layout="wide")

COLOUR = {"New": "#2E7A4F", "Amendment": "#9C6F11", "Deletion": "#B03A30",
          "No action": "#64757A"}

# The three steps. A proposal moves left to right and can never skip a step.
STEPS = ["1 · Proposed changes", "2 · Reviewer sign-off", "3 · Approver sign-off",
         "4 · Excel export"]


# ====== DATA ======

def proposals(status: str | list[str] | None = None) -> pd.DataFrame:
    """The proposed changes, in the order the circulars were read.

    `status` may be a list: the reviewer's queue is two statuses, not one — a change the
    approver sends back is "Changes requested", and it belongs back in front of level 1.
    """
    sql = ("SELECT p.id, p.sr_no, p.change_type, p.department, p.strata, "
           "       p.target_test_code, p.existing_test_description, "
           "       p.proposed_test_description, p.risk_rating, p.status, "
           "       p.reviewer_note, p.approver_note, c.clause_ref, c.text AS clause_text, "
           "       COALESCE(d.title, d.filename) AS circular "
           "FROM proposals p "
           "JOIN clauses c ON c.id = p.clause_id "
           "JOIN documents d ON d.id = p.document_id "
           "WHERE p.change_type != 'No action' ")
    params: tuple = ()
    if status:
        wanted = [status] if isinstance(status, str) else list(status)
        sql += f"AND p.status IN ({', '.join('?' for _ in wanted)}) "
        params = tuple(wanted)
    sql += "ORDER BY p.document_id, c.sequence"
    return pd.DataFrame(store.query(sql, params))


def table(df: pd.DataFrame, pick_column: str | None = None) -> pd.DataFrame:
    """Render the proposed changes as a table. Returns the edited frame if pickable."""
    view = pd.DataFrame({
        "Sr #": df["sr_no"],
        "Change type": df["change_type"],
        "Dept": df["department"],
        "Existing test": df["target_test_code"].fillna("—"),
        "Proposed test / change": df["proposed_test_description"].fillna(""),
        "Risk": df["risk_rating"].fillna(""),
        "Circular": df["circular"],
        "Clause": df["clause_ref"].fillna(""),
        "Status": df["status"],
    })
    if pick_column is None:
        st.dataframe(view, hide_index=True, use_container_width=True, height=430)
        return view

    view.insert(0, pick_column, False)
    edited = st.data_editor(
        view, hide_index=True, use_container_width=True, height=430,
        disabled=[c for c in view.columns if c != pick_column],
        column_config={
            pick_column: st.column_config.CheckboxColumn(pick_column, width="small"),
            "Proposed test / change": st.column_config.TextColumn(width="large"),
        },
        # The key carries the queue length: after a sign-off the queue is shorter, and
        # a stale tick must not carry over onto whatever row now sits in that position.
        key=f"editor_{pick_column}_{len(view)}",
    )
    return edited


def counts_line() -> None:
    """Two rows: what the run produced, then where it has reached in the sign-off."""
    # Row 1 — the pipeline, left to right, the same figures as the full app's dashboard.
    counts = store.counts()
    cols = st.columns(7)
    cols[0].metric("Audit tests indexed", f"{counts['tests']:,}")
    cols[1].metric("Documents received", counts["documents"] + counts["duplicates"])
    cols[2].metric("Duplicates skipped", counts["duplicates"])
    cols[3].metric("Clauses extracted", counts["clauses"])
    cols[4].metric("Actionable", counts["actionable"])
    cols[5].metric("Changes proposed", counts["proposals"])
    cols[6].metric("Approved for export", counts["approved"])

    # Row 2 — the sign-off queue. Where every proposed change has reached.
    by_status = store.approval_counts()
    cols = st.columns(7)
    cols[0].metric("Waiting for reviewer", by_status.get(review.PENDING_L1, 0))
    cols[1].metric("Waiting for approver", by_status.get(review.PENDING_L2, 0))
    cols[2].metric("Approved", by_status.get(review.APPROVED, 0))
    cols[3].metric("Rejected", by_status.get(review.REJECTED, 0))
    cols[4].metric("Sent back", by_status.get(review.CHANGES, 0))


# ====== PAGE ======

st.markdown(
    "<h2 style='margin-bottom:2px'>Circular Processing System - MVP</h2>"
    "<p style='color:#64757A;margin-top:0;font-size:15px'>"
    "Changes proposed from this week's circulars. Reviewer signs off, then approver, "
    "then the Excel file is released.</p>",
    unsafe_allow_html=True)

# store.ready() checks that the TABLES exist, not just the file — SQLite creates an
# empty file on any connection, and a file-only check turns a "run the pipeline first"
# message into "no such table: audit_tests".
if not store.ready():
    st.error("The demo database has not been built yet.")
    st.code("python run_pipeline.py", language="bash")
    st.caption("Run that once — it builds the 500-test library, reads the circulars and "
               "writes the outputs — then reload this page. The database is generated, "
               "so it is not in the repository and every machine builds its own.")
    st.stop()

counts_line()
st.caption(
    "The top row follows the pipeline left to right — what arrived, what was read out "
    "of it, and what the system proposed. The second row is where those proposals have "
    "reached in the sign-off.")
st.caption(
    "**Nothing reaches the Excel export without both sign-offs.** The reviewer's "
    "approval only moves a change forward — it never finalises it. Only the approver's "
    "sign-off makes a change exportable. This is the control the audit team is buying, "
    "so it is enforced in the database, not in this screen.")

step = st.radio("Step", STEPS, horizontal=True, label_visibility="collapsed")
st.divider()


# ---- 1 · WHAT THE SYSTEM PROPOSES ----

if step == STEPS[0]:
    df = proposals()
    st.markdown("**Every change the system is proposing from this week's circulars.**")
    st.caption(
        "One row per proposed change, each traced back to the clause that caused it. "
        "**New** — no existing test covers the obligation. **Amendment** — a test "
        "exists but the requirement has changed. **Deletion** — the circular withdraws "
        "or supersedes something. Every test code shown was checked against the audit "
        "library before it was displayed; a code that could not be validated is dropped, "
        "never guessed.")

    if df.empty:
        st.info("No proposed changes. Run `python run_pipeline.py` first.")
    else:
        chips = df["change_type"].value_counts()
        st.markdown(" &nbsp; ".join(
            f"<span style='background:{COLOUR.get(t, '#555')}1A;color:{COLOUR.get(t, '#555')};"
            f"padding:3px 11px;border-radius:3px;font-size:13px;font-weight:600'>"
            f"{t} &nbsp;{n}</span>" for t, n in chips.items()), unsafe_allow_html=True)
        st.write("")
        table(df)
        with st.expander("Where a change came from"):
            pick = st.selectbox("Proposed change", df["sr_no"], label_visibility="collapsed")
            row = df[df["sr_no"] == pick].iloc[0]
            st.markdown(f"**{row['circular']}** — clause {row['clause_ref'] or '—'}")
            st.info(row["clause_text"])
            st.markdown(f"**Proposed {row['change_type'].lower()}** — "
                        f"{row['proposed_test_description'] or '—'}")


# ---- 2 · REVIEWER ----

elif step == STEPS[1]:
    df = proposals([review.PENDING_L1, review.CHANGES])
    st.markdown("**Reviewer — level 1.**")
    st.caption(
        "Tick the changes you accept and sign them off. They then move to the approver. "
        "A reviewer's sign-off does not release anything; it only advances the change to "
        "level 2. Rejecting stops a change here. Anything the approver has **sent back** "
        "also lands here, marked *Changes requested*, and has to be signed off again.")

    if df.empty:
        st.success("Nothing waiting for review.")
    else:
        edited = table(df, "Accept")
        accepted = edited["Accept"].fillna(False).astype(bool).values
        chosen = df.loc[accepted, "id"].tolist()
        deletions = df.loc[accepted & (df["change_type"] == "Deletion").values]

        note = st.text_input("Reviewer note (optional)", key="note_l1")
        confirm_deletion = True
        if not deletions.empty:
            # Deletions remove an existing audit test. The proposal makes this an
            # explicit human confirmation, never an implicit one.
            confirm_deletion = st.checkbox(
                f"I confirm the {len(deletions)} DELETION(s) selected — these remove "
                f"existing audit tests", key="confirm_del")

        left, right, _ = st.columns([1, 1, 4])
        if left.button(f"Sign off {len(chosen)} change(s)", type="primary",
                       disabled=not chosen or not confirm_deletion):
            review.decide_many(chosen, level=1, decision=review.APPROVED, note=note)
            st.rerun()
        if right.button(f"Reject {len(chosen)}", disabled=not chosen):
            review.decide_many(chosen, level=1, decision=review.REJECTED, note=note)
            st.rerun()


# ---- 3 · APPROVER ----

elif step == STEPS[2]:
    df = proposals(review.PENDING_L2)
    st.markdown("**Approver — level 2.**")
    st.caption(
        "Only changes the reviewer has already signed off appear here, with the "
        "reviewer's note alongside so level 1 can be seen to have happened. Your "
        "sign-off is what releases a change to the Excel export.")

    if df.empty:
        st.info("Nothing waiting for approval. Changes appear here once the reviewer "
                "has signed them off in step 2.")
    else:
        edited = table(df, "Approve")
        chosen = df.loc[edited["Approve"].fillna(False).astype(bool).values, "id"].tolist()

        with st.expander("What the reviewer said"):
            st.dataframe(pd.DataFrame({"Sr #": df["sr_no"],
                                       "Reviewer note": df["reviewer_note"].fillna("—")}),
                         hide_index=True, use_container_width=True)

        note = st.text_input("Approver note (optional)", key="note_l2")
        left, right, _ = st.columns([1, 1, 4])
        if left.button(f"Approve {len(chosen)} change(s)", type="primary", disabled=not chosen):
            review.decide_many(chosen, level=2, decision=review.APPROVED, note=note)
            st.rerun()
        if right.button(f"Send back / reject {len(chosen)}", disabled=not chosen):
            review.decide_many(chosen, level=2, decision=review.CHANGES, note=note)
            st.rerun()


# ---- 4 · EXPORT ----

else:
    approved = proposals(review.APPROVED)
    st.markdown("**Excel export — approved changes only.**")
    st.caption(
        "The formatted hand-off file for eAudit. It contains the approved changes and "
        "nothing else: anything still waiting for a sign-off is named below and left "
        "out. Excel is an output here, not the database — editing the downloaded file "
        "changes nothing in the system.")

    if approved.empty:
        st.warning("Nothing has been approved yet, so there is nothing to export.")
    else:
        table(approved)

    path, refusals = review.export_eaudit()
    st.write("")

    if path:
        st.download_button(
            f"Download the eAudit export — {len(approved)} approved change(s)",
            data=path.read_bytes(), file_name=path.name, type="primary",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption(f"Written to `{path}` — formatted headings, column widths, one row "
                   f"per approved change, each carrying who approved it and when.")

    if refusals:
        with st.expander(f"Held back — {len(refusals)} change(s) not yet approved"):
            st.dataframe(pd.DataFrame({"Not exported": refusals}),
                         hide_index=True, use_container_width=True)
            st.caption("The delivered system refuses the export outright while anything "
                       "in scope is unapproved. The demo exports the approved rows and "
                       "names the rest, so the check is visible.")

    st.divider()
    if st.button("Reset all sign-offs (demo only)"):
        review.reset_all()
        st.rerun()
