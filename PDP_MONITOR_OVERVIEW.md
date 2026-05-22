# PDP Monitor — Overview

## What We Built
An automated audit tool that scrapes every Man Matters product page, scores it across five dimensions using Claude AI, and produces a single interactive HTML report. The goal: catch copy drift, visual gaps, and narrative misalignment before they hurt conversion — without anyone manually reviewing a PDP.

---

## How the Score Works

Every PDP gets an **Overall Score /10**, weighted across five tabs:

| Tab | Weight | What it checks |
|-----|--------|----------------|
| **Hygiene Check** | 15% | Claims accuracy, brand voice violations, spelling & grammar |
| **Narrative × Persona** | 30% | How well the page executes the configured narrative for the target persona (Tejas / Aakash / Fitness Buyer) |
| **Visual Layer** | 30% | Hero images, carousel flow, ingredient shots, proof points, lifestyle imagery — scored via Claude Vision directly on Zeus CDN images |
| **Text Layer** | 15% | Copy insights: forbidden words, missing hooks, weak CTAs, tone mismatches |
| **Reviews & Ratings** | 15% | Review freshness, rating distribution, theme alignment with the narrative |

Tab scores roll up into the PDP Overall, and all PDP scores average into the **Product Health Score** shown in the hero bar.

---

## Tick / Untick — What It Does

Every flag and suggestion in the report is actionable:

- **SSR suggestion rows** (Narrative, Visual, Reviews tabs) — each row is a scored sub-dimension (e.g. Hero/Banner, Carousel Flow). Marking a suggestion as *resolved* sets that sub-dimension to **10/10**, which immediately recalculates the tab score → PDP overall → Product Health Score at the top.
- **Hygiene flags** (Claims / Brand / Spell sub-tabs) — dismissing individual flags scales the sub-score linearly from its current value toward **10/10** as more items are cleared.
- **Text Layer insights** — same linear scaling; resolving critical and warning items lifts the copy score toward 10.

The score cascade is live: one tick in Reviews ripples up to the 5.4 hero bar in real time — so the report doubles as a prioritisation worksheet. The team can see exactly how much each fix is worth.

---

## Images — Pulled Directly from Zeus CMS

The visual audit does not rely on screenshots. Zeus CMS cache files (`outputs/zeus_cache/{page_id}.json`) are loaded at run time and every image — hero gallery, clinical proof slides, ingredient carousels, comparison banners — is downloaded from the Zeus CDN and sent to Claude Vision for analysis. This means the scorer sees the same images the customer sees, in page display order, and can flag missing proof-point images, weak lifestyle shots, or broken hierarchy with exact positional context.

---

## Reviews — Dates and Ratings from Zeus

Customer reviews are fetched from the same Zeus cache rather than scraped from the storefront (which renders them dynamically and inconsistently). Each review card in the report shows the **author, star rating, date, and title** exactly as stored in Zeus — e.g. Rudra · ★★★★★ · 10 May 2026. The scorer then evaluates freshness (how recent the reviews are), rating distribution, and whether review language aligns with the page's configured narrative — giving a factual read on social proof health rather than guessing from a scraped snapshot.
