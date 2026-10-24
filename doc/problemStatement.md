# Problem Statement — Groww Weekly Review Pulse

## Overview

Build an agent that turns public Google Play Store reviews of **Groww** (Stocks, Mutual Funds, IPO) into a **weekly one-page pulse**. A team should be able to read it in a few minutes and learn three things: what users care about, what they actually said, and what to do next.

The pulse goes out through two tools the team already uses:
- **Google Docs**: the written pulse
- **Gmail**: a draft email to yourself (or an alias) that contains the pulse or links to it

Both integrations go through **MCP servers**. No custom OAuth or REST code.

## Target Product

| Field | Value |
|-------|-------|
| App | Groww: Stocks, Mutual Fund, IPO |
| Play Store ID | `com.nextbillion.groww` |
| Play Store URL | https://play.google.com/store/apps/details?id=com.nextbillion.groww&hl=en_IN |
| Sources | Google Play Store (public reviews) |

> **Scope note:** The original brief mentions both the App Store and the Play Store. **Apple App Store reviews are out of scope for now.** v1 uses Google Play Store reviews only. The App Store may be added later as an extra source.

## End-to-End Flow ("Done" Looks Like)

1. **Ingest**: pull recent Google Play Store reviews, following the constraints below.
2. **Cluster**: group the reviews into a small set of themes (at most 5).
3. **Summarize**: write a one-page weekly note.
4. **Publish**: create or update the pulse in **Google Docs** (via MCP).
5. **Deliver**: create a **Gmail draft** to yourself or an alias that contains the note or links to it (via MCP).

## Functional Requirements

### 1. Review Import
- Time window: roughly the **last 8–12 weeks**.
- Fields, as far as the export provides them: `rating`, `title`, `text`, `date`.
- Source: Google Play Store, public review exports only. The App Store is out of scope for now.

### 2. Theme Clustering
- **At most 5 themes** in total.
- Pick themes that fit Groww. Examples: onboarding, KYC, payments, statements, withdrawals, app performance, customer support, charges/fees.

### 3. Weekly One-Page Note
Must contain:
- **Top 3 themes**: chosen from the clustered themes, ranked by how much users talk about them
- **3 user quotes**: verbatim snippets from real reviews, with no invented or reworded text
- **3 action ideas**: concrete next steps that follow from the themes

### 4. Delivery
- Write the note to a Google Doc using a Google Docs MCP server.
- Create a Gmail draft (not sent) to yourself or an alias, using a Gmail MCP server. The draft contains the note or a clear link to the Doc.

## Integration Requirements (MCP-First)

- Use **MCP (Model Context Protocol) servers** for Google Docs and Gmail.
- **Do not** call Google APIs directly as the main integration path. That means no custom OAuth client and no REST client code.
- Use whichever Docs and Gmail MCP servers or connectors the environment provides. Auth and HTTP handling stay inside the MCP layer.

## Key Constraints

| Area | Constraint |
|------|------------|
| **Reviews** | Public review exports only. No scraping behind store logins and no automation that breaks store Terms of Service. |
| **Themes** | At most 5 themes. The pulse highlights the top 3. |
| **Length** | The note stays scannable, **≤ 250 words** where applicable. |
| **Privacy** | **No PII** in any artifact: no usernames, emails, device IDs or other details that identify a reviewer. Quotes must be anonymous, with identifying details removed. |
| **Quotes** | Verbatim only. Never invent or paraphrase text inside quotes. |

## Audience & Value

| Audience | Why It Helps |
|----------|--------------|
| Product / Growth | Prioritize fixes and improvements based on real user signals |
| Support | Match messaging to what users are actually saying |
| Leadership | A one-page health check without reading through raw reviews |

## Deliverables Checklist

- [ ] Play Store review ingestion for the last 8–12 weeks (public sources only)
- [ ] Clustering into ≤ 5 themes
- [ ] One-page weekly pulse (≤ 250 words) with top 3 themes, 3 verbatim quotes and 3 action ideas
- [ ] PII removed from all outputs
- [ ] Pulse published to Google Docs via MCP
- [ ] Gmail draft created via MCP, containing or linking to the pulse

## Suggested Pulse Template

```
Groww — Weekly Review Pulse (Week of <date>)
Window: <start> – <end> | Reviews analyzed: <n> | Avg rating: <x.x>

Top Themes
1. <Theme> — <share %> — <one-line summary>
2. <Theme> — <share %> — <one-line summary>
3. <Theme> — <share %> — <one-line summary>

What Users Said
- "<verbatim quote>" (★<rating>)
- "<verbatim quote>" (★<rating>)
- "<verbatim quote>" (★<rating>)

Action Ideas
1. <Concrete next step tied to theme 1>
2. <Concrete next step tied to theme 2>
3. <Concrete next step tied to theme 3>
```
