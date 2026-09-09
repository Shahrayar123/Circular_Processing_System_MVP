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

from mvp import config, excel_out, review, store  # noqa: E402

LOGO = Path(__file__).resolve().parent / ".streamlit" / "ABL.PK_BIG.svg"

st.set_page_config(page_title="Circular Processing System — Allied Bank",
                   page_icon="✅", layout="wide")
st.logo(str(LOGO), size="large", link="https://www.abl.com")

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
           "       p.existing_risk_rating, p.existing_department, "
           "       p.rationale, p.decided_at, "
           "       p.obligation_index, o.text AS obligation_text, "
           "       (SELECT COUNT(*) FROM obligations x WHERE x.clause_id = c.id) AS obligation_count, "
           "       p.reviewer_note, p.approver_note, c.clause_ref, c.text AS clause_text, "
           "       COALESCE(d.title, d.filename) AS circular "
           "FROM proposals p "
           "LEFT JOIN obligations o ON o.id = p.obligation_id "
           "JOIN clauses c ON c.id = p.clause_id "
           "JOIN documents d ON d.id = p.document_id "
           f"WHERE {store.is_change('p')} ")
    params: tuple = ()
    if status:
        wanted = [status] if isinstance(status, str) else list(status)
        sql += f"AND p.status IN ({', '.join('?' for _ in wanted)}) "
        params = tuple(wanted)
    sql += "ORDER BY p.document_id, c.sequence"
    return pd.DataFrame(store.query(sql, params))


def _change_column(df: pd.DataFrame) -> pd.Series:
    """What this proposal asks the reviewer to accept, in one cell.

    A New or an Amendment proposes wording, so that is what goes here. A DELETION proposes
    no wording at all — it removes a test — and the cell was therefore left empty, which
    told the reviewer nothing and looked like missing data.

    Worse than looking empty: the row showed only the test CODE, so a reviewer was being
    asked to approve removing a control without seeing what the control said. Deletions
    are the highest-consequence change this system makes and the one it must never
    auto-approve; the description is already stored frozen on the proposal, so show it.
    """
    proposed = df["proposed_test_description"].fillna("")
    removing = ("Remove — " + df["existing_test_description"].fillna("(test wording not "
                                                                    "recorded)"))
    return proposed.where(df["change_type"] != "Deletion", removing)


def table(df: pd.DataFrame, pick_column: str | None = None) -> pd.DataFrame:
    """Render the proposed changes as a table. Returns the edited frame if pickable."""
    view = pd.DataFrame({
        "Sr #": df["sr_no"],
        "Change type": df["change_type"],
        "Dept": df["department"],
        # The clause the circular actually says, next to what we propose to do about it.
        # Every proposal cites its source clause — that is the grounding rule — and a
        # reviewer cannot check a proposal against a citation they have to go and look up.
        # It matters most for a DELETION, where the clause is the withdrawal itself
        # ("... shall stand withdrawn") and is the only evidence the removal is justified.
        "From the circular": df["clause_text"].fillna(""),
        "Existing test": df["target_test_code"].fillna("—"),
        "Proposed test / change": _change_column(df),
        "Circular": df["circular"],
        "Clause": df["clause_ref"].fillna(""),
        "Status": df["status"],
    })
    if pick_column is None:
        st.dataframe(view, hide_index=True, use_container_width=True, height=430)
        return view

    view.insert(0, pick_column, False)
    # The proposed wording is editable in place: an auditor correcting a draft test is
    # the normal case, and forcing them to reject-and-wait would be theatre. Every edit
    # is written to proposal_revisions before the proposal changes.
    editable = {pick_column}
    if pick_column == "Accept":
        editable.add("Proposed test / change")
    edited = st.data_editor(
        view, hide_index=True, use_container_width=True, height=430,
        disabled=[c for c in view.columns if c not in editable],
        column_config={
            pick_column: st.column_config.CheckboxColumn(pick_column, width="small"),
            # Read-only by construction: it is not in `editable`. The circular's own words
            # are evidence, and evidence a reviewer can retype is not evidence.
            "From the circular": st.column_config.TextColumn(width="large"),
            "Proposed test / change": st.column_config.TextColumn(width="large"),
        },
        # The key carries the queue length: after a sign-off the queue is shorter, and
        # a stale tick must not carry over onto whatever row now sits in that position.
        key=f"editor_{pick_column}_{len(view)}",
    )
    return edited


def counts_line() -> None:
    """One row. Every figure answers a question the client actually asks.

    There were two rows and twelve figures. "Approved" appeared in both, under two
    names, for the same number — and "Clauses extracted / Actionable" invited the
    question "so which is it?" without either number being useful on this screen. A
    figure nobody can act on is not information, it is doubt.
    """
    counts = store.counts()
    by_status = store.approval_counts()

    cols = st.columns(5)
    cols[0].metric("Circulars received", counts["documents"] + counts["duplicates"])
    cols[1].metric("Changes proposed", counts["proposals"])
    cols[2].metric("Waiting for reviewer", by_status.get(review.PENDING_L1, 0)
                   + by_status.get(review.CHANGES, 0))
    cols[3].metric("Waiting for approver", by_status.get(review.PENDING_L2, 0))
    cols[4].metric("Approved — ready to export", by_status.get(review.APPROVED, 0))

    rejected = by_status.get(review.REJECTED, 0)
    if rejected:
        st.caption(f"{rejected} change(s) rejected and closed.")


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
        with st.expander("Where a change came from — and what exactly changes"):
            pick = st.selectbox("Proposed change", df["sr_no"], label_visibility="collapsed")
            row = df[df["sr_no"] == pick].iloc[0]
            st.markdown(f"**{row['circular']}** — clause {row['clause_ref'] or '—'}")
            st.info(row["clause_text"])

            # A clause carrying several duties produces several proposals, all sharing one
            # source reference. Say which duty this row is, or the reviewer sees the same
            # clause three times and assumes it is a duplicate.
            if (row.get("obligation_count") or 1) > 1:
                st.warning(
                    f"This clause states **{int(row['obligation_count'])} separate "
                    f"obligations**, and each is proposed and approved on its own. This is "
                    f"obligation **{int(row['obligation_index'])}**:", icon="⚖️")
                st.markdown(f"> {row['obligation_text']}")

            # Before and after, side by side. The "before" is the snapshot taken when the
            # decision was made, not a live read of the library — the library moves, and
            # the reviewer has to see what they are actually approving a change against.
            before, after = st.columns(2)
            with before:
                st.markdown("**Existing test — as the library holds it**")
                if row["target_test_code"]:
                    st.markdown(f"`{row['target_test_code']}`  ·  "
                                f"{row['existing_department'] or '—'}  ·  risk "
                                f"{row['existing_risk_rating'] or '—'}")
                    st.write(row["existing_test_description"] or "—")
                else:
                    st.caption("None — no existing test covers this obligation, which is "
                               "why the proposal is a new test.")
            with after:
                st.markdown(f"**After the proposed {row['change_type'].lower()}**")
                st.markdown(f"{row['department'] or '—'}  ·  risk "
                            f"{row['risk_rating'] or '—'}")
                st.write(row["proposed_test_description"] or
                         "— (a deletion removes the test above)")

            st.caption(f"Why: {row['rationale']}")
            st.caption(f"Decided {row['decided_at'] or '—'}. Every field above is stored "
                       f"with the proposal, so what the system proposed can always be "
                       f"compared with what was finally approved.")

            edits = review.revisions(int(row["id"]))
            if edits:
                st.markdown("**Changed by a human since**")
                for e in edits:
                    st.markdown(
                        f"- *{review.EDITABLE.get(e['field'], e['field'])}* — "
                        f"{e['changed_by']}, {e['changed_at']}"
                        + (f" ({e['note']})" if e["note"] else ""))
                    st.caption(f"from: {(e['old_value'] or '—')[:300]}")
                    st.caption(f"to:  {(e['new_value'] or '—')[:300]}")


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

        # An edit is recorded whether the row is signed off or not — the reviewer
        # changed the wording either way, and losing that silently is the one thing an
        # audit trail exists to prevent.
        amended = 0
        for position, proposal_id in enumerate(df["id"]):
            # A Deletion's cell shows what is being REMOVED, not wording being proposed.
            # Saving it back would write "Remove — Check that ..." into
            # proposed_test_description and it would reach the eAudit export as though a
            # reviewer had drafted it.
            if df["change_type"].iloc[position] == "Deletion":
                continue
            new_text = str(edited["Proposed test / change"].iloc[position] or "")
            if review.edit(int(proposal_id), "proposed_test_description", new_text,
                           level=1, note=note):
                amended += 1
        if amended:
            st.caption(f"{amended} wording change(s) recorded against the original.")

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
    st.markdown("**Two Excel files come out of this, and they are not the same file.**")
    st.caption(
        "Excel is an output here, not the database — editing either downloaded file "
        "changes nothing in the system.")

    st.markdown("| File | What it holds |\n"
        "|---|---|\n"
        "| **Audit Checklist Working File** | Every proposed change under review, "
        "approved or not |\n"
        "| **eAudit BAC Export** | Approved changes ONLY, the quarterly hand-off |\n")
    st.write("")

    # The working file is regenerated on the spot, so what downloads is the current
    # state of the review rather than whatever the last pipeline run happened to leave
    # on disk — someone may have approved three more changes since.
    working = excel_out.build()
    # Every proposed change, not just the approved ones — that is what the working file
    # is for, and the count on the button has to match what is inside it.
    all_changes = proposals()
    left, right = st.columns(2)
    with left:
        st.download_button(
            f"Download the working file — {len(all_changes)} proposed change(s)",
            data=working.read_bytes(), file_name=working.name, type="primary",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        st.caption(f"`{working.name}` — Summary, Proposed Tests, Annexure and Week MIS, "
                   f"laid out exactly as ABL's own working file.")

    path, refusals = review.export_eaudit()
    with right:
        if path:
            st.download_button(
                f"Download the eAudit export — {len(approved)} approved change(s)",
                data=path.read_bytes(), file_name=path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            st.caption(f"`{path.name}` — one row per approved change, each carrying who "
                       f"approved it and when. **One sheet by design.**")
        else:
            st.button("Download the eAudit export", disabled=True)
            st.caption("Nothing approved yet, so there is nothing to hand off.")

    st.write("")
    if approved.empty:
        st.warning("Nothing has been approved yet. The working file below still contains "
                   "every proposed change; the eAudit export would be empty.")
    else:
        table(approved)

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
