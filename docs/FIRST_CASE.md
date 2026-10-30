# Your first case: a spending ceiling, not a payment

Start with `launch.py` in offline mode. Allow about 15 minutes for the first pass. The tutorial uses short transcriptions of two public City of Exeter records; it does not authenticate them. The files are bundled in `tutorial/`. Read them before declaring mappings reviewed.

1. **Create a case** at `/workbench`. Use case title `Bar-screen tutorial`, project `Wastewater mechanical bar screen`, municipality `Exeter, California`, jurisdiction slug `exeter-ca`, publisher `City of Exeter`, allowed hostname `cityofexeter.ca.gov`. A slug is a short machine name: lowercase letters/digits with hyphens; no spaces or URL. Untick importing the first source now if you want to add it separately. Submit **Create case**.

2. **Add the minutes source.** Choose `tutorial/01-bar-screen-council-minutes.txt`. Title: `July 28, 2026 City Council Action Minutes`; publisher stays `City of Exeter`; document role `ADOPTED_ACTION_MINUTES`; document date and as-of date `2026-07-28`; anchor `PDF pages 3-4; I.10 and consent-calendar adoption`. URL:
   `https://cityofexeter.ca.gov/wp-content/uploads/2026/08/item-i.1-july-28-2026-minutes-signed.pdf`
   Wait for the filename/text to populate, then **Add source**. A declared URL is not a fetch. The capture label must remain user-supplied excerpt/unverified.

3. **Add the tracker source.** Choose `tutorial/02-bar-screen-project-tracker.txt`. Title `City of Exeter - City Project Tracker`; role `CITY_INFORMATIONAL_TRACKER`; document/as-of date `2026-10-23`; anchor `HTML project table row 19; page last-updated statement`. URL:
   `https://cityofexeter.ca.gov/city-of-exeter-city-council-project-tracker/`
   Submit **Add source**. Both roles are needed for live eligibility, even when only one supports the claim.

4. **Map the authorization.** Open the minutes source. Copy its displayed preserved text into **Exact excerpt** (the whole short tutorial file is acceptable). Enter its anchor, predicate `AUTHORIZED_USE_CEILING`, subject `exeter-wastewater-mechanical-bar-screen`, action `2026-07-28-I.10`, amount in cents `41452978`, qualifier `UP_TO`. Leave phase blank. Submit **Add mapping**. Amounts are integer cents: $414,529.78 is 41452978, not 414529.78. The excerpt must actually occur in the imported text; a paraphrase is refused.

5. **Map the tracker.** Open the tracker source and copy its preserved text into **Exact excerpt**. Use predicate `CITY_REPORTS_PHASE`, the same subject, action `TRACKER-ROW-19`, phase `CONTRACT_AWARDED`; leave amount/qualifier blank. Add mapping. The source date supplies the as-of context.

6. **Review each mapping.** Open the mapping, read the original displayed text and structured values, select `HUMAN_REVIEWED`, enter your reviewer label, and click **Record review state**. Adding/editing a mapping does not review it. Do not use a real personal name if a local reviewer alias is sufficient. Editing a source or mapping later invalidates dependent review/confirmation as appropriate.

7. **Create the authorization claim.** Choose the `AUTHORIZED_USE_CEILING` claim template. Text: `Council authorized up to $414,529.78 for the wastewater mechanical bar screen.` Use the subject/action and integer amount from step 4, tick the ceiling option, and select only the minutes authorization fact as supporting evidence. Do not select the tracker fact just because it was read. In **What record would settle this claim?**, enter `Official signed council authorization record`. This field is required. Add the claim, open it, read its plain-language confirmation, enter your reviewer label and tick the attestation, then **Record confirmation**.

8. **Optionally add the tracker claim.** Use the `CITY_REPORTS_PHASE` template. Text: `The city reported the wastewater mechanical bar screen as Contract Awarded as of October 23, 2026.` Use the same subject, action `TRACKER-ROW-19`, phase `CONTRACT_AWARDED`, as-of `2026-10-23`, and only the tracker fact. In **What record would settle this claim?**, enter `Dated city tracker and subsequent field inspection record`. Add and confirm it separately.

9. **Investigate offline.** Open the case's investigation page. With correct mappings the expected deterministic outcomes are `DOCUMENTED_ACTION` for authorization and `CITY_REPORTED_STATUS` for the tracker claim. These are Court results over your mappings, not a model run. Read the reason codes and citation provenance. Neither result establishes payment, installation, inspection or physical completion.

10. **Try the real investigator only when ready.** Stop the server and use the README's `--live` instructions. Reopen the same case, refresh the investigation page to use its latest revision, select the confirmed authorization claim and click the live selected-case button once. Both reviewed source roles are required. The model chooses reads and proposed citations; Court can still refuse. Inspect proposed IDs separately from contextual citations and verify request/usage counts. A refused/failed result is not permission for another paid attempt.

11. **Restart and back up.** Stop with Ctrl+C and restart using the same data directory. Your case should reappear. Back up the entire directory while the server is stopped, including spending state. Do not delete/recreate the ledger to replenish budget. Use [Troubleshooting](TROUBLESHOOTING.md) for stale revisions or disabled buttons.
