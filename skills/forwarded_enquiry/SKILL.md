---
name: forwarded-enquiry
description: Handle a WhatsApp handoff about one specific forwarded Listing; use instead of general search discovery when an Enquiry/Listing context exists.
---

# Forwarded Listing Enquiry

Use for a specific Listing enquiry forwarded by Gwen/another internal agent through the
Enquiry/WhatsApp handoff. The flow may identify the Listing and Rent/Let vs Buy/Sell,
create/link the Enquiry and Lead, gather profile facts, and progress toward viewing. This
skill—not Property Search—is authoritative unless the enquirer explicitly asks for alternatives.

## Stay with the specific Listing

Default progression: answer Listing questions → collect useful profile facts → judge whether
the profile is sufficient for this Listing → ask when they want to view → later progress with
the owner/listing agent. Keep replies concise and never add unrelated questions just to continue.

Do not proactively ask about neighbourhoods, condos, work/school destinations, searching
Greater KL, or other areas/Listings. Do not broaden the search without an explicit request.
This applies to agent and direct leads.

Good: “Thanks — I've got the profile. When would you like to view the property?”
Bad: “Which neighbourhoods or condos would you like me to target?”
For “What floor is the unit?”, answer from grounded Listing context without area discovery.

## Passive profile collection

Extraction/persistence is passive. A message may contain no profile facts, some facts, only a
Listing question, or both. Answer questions even when no profile facts are supplied. For
“Budget is 15k. What floor is it?”, acknowledge briefly and answer the floor question; do not
turn the exchange into a form or claim a value was saved unless that outcome is known.

Profile sufficiency is contextual judgment, not a rigid checklist. It means enough information
to credibly progress this Listing. Nationality, household/pax, occupation, pets, timing, budget,
bedrooms, furnishing, and viewing preference can help, but none is universally mandatory. Use
known Lead, Enquiry, Listing, and conversation context. Do not re-ask clear information or ask
optional questions to fill Bubble fields. Ask only the smallest materially useful follow-up;
otherwise progress to viewing. Never invent missing values.

## Transaction and budgets

`Enquiry.TransactionType` is the current Enquiry's source of truth: a text list containing only
`Rent/Let` or `Buy/Sell`. Resolve it from explicit Gwen instruction, otherwise Listing context;
if ambiguous, ask Gwen/internal sender, never the enquirer. A `Rent/Let` budget belongs only in
`Lead.budgetRent`; a `Buy/Sell` budget only in `Lead.budgetBuy`; never write one budget to both.
`Lead.TransactionType` may cumulatively contain both allowed values. Ignore unknown values.

## Agent and direct leads

For `Lead.Agent? = Yes`, treat them as an agent representing a prospect and progressing this
Listing: answer operational questions, accept the represented profile, and move toward viewing;
never use consumer discovery. For `No`, keep the same initial focus. Broader recommendations
become relevant only when the Listing is unsuitable or the lead explicitly requests them.

## Capability boundaries

Current implementation does not automatically contact landlords/listing agents, determine
owner acceptance, or schedule confirmed viewings. Those are future stages. Never claim they
happened without a completed tool/action; request a preferred viewing time and describe the
actual next step accurately.
