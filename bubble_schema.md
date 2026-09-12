# Bubble Schema

IMPORTANT:
Bubble field names recorded here should be treated as case-sensitive by Python.

When a Bubble field name has been confirmed, Python should use exactly the spelling/capitalization recorded here.

Do not silently normalize or invent Bubble field keys.

When future Bubble schema changes are confirmed, update this file in the same code change where practical.

## Conversation

Purpose:
A logical Rentee communication thread between a Principal and Counterparty, optionally concerning one Enquiry.

Fields:

| Field | Type | Notes |
|---|---|---|
| CounterParty Phone | text | Exact Bubble field name. Normalized WhatsApp counterparty phone |
| ActiveSkill | text | `create_listing` while an agent is creating inventory over WhatsApp |
| CounterParty Role | text | Exact Bubble field name |
| Counterparty User | User | Optional relationship to User |
| Enquiry | Enquiry | Optional enquiry-specific conversation |
| Last Inbound At | date | Latest inbound activity |
| Last Outbound At | date | Latest outbound activity |
| Lead | Lead | Convenience relationship copied from Enquiry.Lead for enquiry-specific Conversations |
| Listing | Listing | Convenience relationship copied from Enquiry.Listing for enquiry-specific Conversations |
| Previous Response ID | text | OpenAI response continuity for this logical Conversation |
| Principal | User | Who Rentee is acting for |
| Rentee Role | text | Role/skills Rentee performs in this Conversation |
| Status | text | Current expected values: Active / Closed |
| Subject | text | Optional human-readable label |

Conversation semantics:
- logical identity is approximately Principal + CounterParty Phone + Enquiry
- Enquiry is optional
- an enquiry-specific Conversation remains tied to that Enquiry
- do not switch an existing enquiry-specific Conversation to another Enquiry
- create/find another Conversation instead
- one physical WhatsApp chat can therefore contain multiple logical Rentee Conversations
- a general Conversation may exist with Enquiry empty
- Conversation.Enquiry remains the authoritative transaction link; Lead and Listing are denormalized conveniences
- a principal-side Conversation is created only when Rentee actually communicates with the Principal about that Enquiry

## Enquiry

Purpose:
Shared transaction state. Multiple Conversations can coordinate through the same Enquiry.

Known fields:

| Field | Type | Notes |
|---|---|---|
| Principal | User | Who Rentee is acting for in this transaction |
| Agent | User | Existing |
| Agent? | text | Expected values Yes / No |
| Enquirer Phone | text | Existing |
| Handoff Code | text | Existing |
| Lead | Lead | Relationship |
| Listing | Listing | Relationship |
| Original Enquiry | text | Existing |
| TransactionType | list of text | Known values Rent/Let and Buy/Sell |
| OwnerCheckStatus | text | Known values Pending / Sent / Replied |
| OwnerCheckPhone | text | Existing |
| OwnerCheckSentAt | date | Existing |
| OwnerCheckResponse | text | Existing |

## Message

Purpose:
One persisted communication event.

Known fields:

| Field | Type | Notes |
|---|---|---|
| Conversation | Conversation | Logical Conversation containing this Message |
| listing | Listing | Optional exact listing referred to by this message |
| phone | text | Existing exact field name |
| direction | text | Exact values Inbound / Outbound |
| whatsappMessageId | text | Meta WhatsApp message ID |
| lead | Lead | Existing relationship |
| messageContent | text | Message content |
| response_ID | text | Existing OpenAI response ID |
| own_Sent? | text | Human vs AI authorship |

Message semantics:
- direction describes transport direction relative to Rentee
- own_Sent? describes human vs AI authorship
- normal inbound human = Inbound + Yes
- AI outbound = Outbound + No
- every Message newly persisted by Python must have exactly one Conversation
- historical Message records without Conversation remain readable during migration
- Meta reply context resolves through Message.whatsappMessageId to Message.Conversation
- `listing` is an existing optional Bubble relationship to Listing; its exact field name is lowercase and it identifies the property referred to by that particular Message, not the Conversation as a whole

## Listing

| Field | Notes |
|---|---|
| condo | Existing |
| Geo | Geo relationship |
| unitNumber | text |
| propertyType | text; `Condo` or `Landed` |
| TransactionType | list; known values `Rent/Let` and `Buy/Sell` |
| beds | Existing |
| priceRent | Existing |
| priceSale | Existing |
| Sq Ft | Existing |
| Furnishing | Existing canonical furnishing label |
| sourceURL | Existing |
| owner | User |
| ownerContact | Exact known Bubble API field key; lowercase o |
| availability | Existing |
| availability_date | Existing |
| photos | list of images |
| coverPhoto | image |
| photoUploadBuffer | image (temporary WhatsApp upload buffer; cleared after attachment) |
| Description | text |
| Notes | text |

Important:
ownerContact is currently the authoritative property-side destination used by the owner-check workflow.

## Lead

Agent?
owner
TransactionType
ActiveForwardedEnquiry
bedroomsMin
budgetRent
budgetBuy
nationality
adults
children
helpers
furnishingPreference
occupation
pets
startDate
viewingPreference

## Folio

Known fields:

lead
folioItems

## Geo

| API field | Type | Source / use |
|---|---|---|
| Name | text | User-specified Geo display field. Existing readers also support legacy `name` records. |
| Adjacent_geos | list of Geo relationships | Exact case-sensitive key supplied by the user for the populated Bubble field. Read only to offer broader scope after current-scope exhaustion. |

## Condo

| API field | Type | Source / use |
|---|---|---|
| Geo | Geo relationship | User-confirmed relationship; existing `get_geo_condo_ids` queries this exact key. |

Field verification: these spellings follow the supplied Bubble schema and existing access patterns. No authenticated live schema response was available in the development environment during this change. Do not infer adjacency from coordinates or replace `Adjacent_geos` with a Python map.

Search memory stored in Lead.searchActive and Lead.searchBriefJSON now preserves `geography_provenance` (explicit source, user evidence and last explicit area/Condo snapshot). `pending_broadening` is a separate one-use offer bound to the Folio and current scope, expiring after 15 minutes; it does not constrain retrieval until accepted. `scope_needs_clarification` prevents a removed invalid-only scope from becoming an unrestricted Listing query. These are JSON members, not new Bubble columns.
