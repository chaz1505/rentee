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
| Awaiting Viewing Response | text | Yes while the enquirer is answering a viewing-time prompt |

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
| OwnerCheckResult | text | Values available / unavailable / unclear |
| OwnerCheckReason | text | Optional internal decline reason; never customer-facing |
| OwnerCheckViewingNote | text | Optional safe, grounded viewing restriction |
| OwnerCheckNotifiedAt | date | Durable enquirer-notification idempotency marker |
| OwnerCheckNotificationConversation | Conversation | Enquirer Conversation notified of the result |
| OwnerCheckResult | text | Values available / unavailable / unclear |
| OwnerCheckReason | text | Optional internal decline reason; never customer-facing |
| OwnerCheckViewingNote | text | Optional safe, grounded viewing restriction |
| OwnerCheckNotifiedAt | date | Durable enquirer-notification idempotency marker |
| OwnerCheckNotificationConversation | Conversation | Enquirer Conversation notified of the result |

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
| beds | Existing |
| priceRent | Existing |
| sourceURL | Existing |
| owner | User |
| ownerContact | Exact known Bubble API field key; lowercase o |
| availability | Existing |
| availability_date | Existing |

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
