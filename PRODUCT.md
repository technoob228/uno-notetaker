# Product

<!-- impeccable:product-schema 1 -->

## Platform

web (responsive: desktop inside the Uno Work app frame, phone as an installable PWA)

## Stack

Plain HTML/CSS/JS served by the FastAPI app (`app/static`), no build step, no framework. Must keep working inside a third-party iframe (Uno Work) and standalone.

## Users

Primary: a client of Uno Work who is not a developer — a founder, salesperson, recruiter, consultant. They open Notetaker inside Uno Work (their AI computer) and want notes from their meetings without setting anything up. Many calls a week; they come back after a call to read the summary, tick action items, share notes, or ask a question about the meeting. Secondary: the same person on the phone (voice memo, meeting in the room).

## Purpose

Meeting notes on the person's own Uno computer — a self-hosted Granola / Otter. A bot ("Uno Notetaker", or the person's assistant name) joins calls from their calendar (Google / Outlook & Teams connected in the Uno account, or any iCal link), or they record a tab / the room / upload a file. Out come a transcript with speakers, notes (summary, decisions, action items with owners, open questions), "Ask about this meeting", a share link, and action items that can be handed to Uno ("Ask Uno to do it"). Every meeting is a folder in `~/Meetings`.

Success: the person opens it, immediately sees their meetings and what's next, reads clean notes, and never has to understand the plumbing (providers, models, keys).

## Capabilities to preserve

Meetings list with search across all meetings; meeting page (notes, transcript with timestamps and speakers, rename speaker, talk time, audio, rewrite notes by kind, Ask, share, delete); Action items view across meetings (checkboxes, Ask Uno to do it); Calls (bot status, upcoming calls with per-call switch, recent calls with what happened / what the bot saw, send the bot by link); Record (call in another tab with consent line / mic only), Upload; Settings (notifications Inbox/Telegram, Telegram pairing, calendars from Uno account + iCal with guides, bot rules and name, AI provider and speech-to-text, defaults); Home widget; phone layout with record/upload bar; login page; public share page.

## Decisions (Misha, 30.10.2026)

- Main screen: meetings → notes (Granola / Fathom pattern): meetings by day, a clean notes document; upcoming calls with the bot at the top of the feed.
- Rare settings (AI provider, speech-to-text model, keys, bot wait minutes) go under "Advanced"; on view: calendars, the bot, notifications.
- The current look is rejected as "scary and inconvenient".

## Voice

Plain, short, friendly English UI copy (the product UI is English); no jargon (no "STT", "endpoint" on the surface). Honest about what the bot does (it is visible in the call, it says it records).

## Brand commitments

- Visual direction (Misha, 30.10.2026): the category standard played straight — "classic, like Granola". Craft bar: Granola, Fathom; a calm meetings list by day, a clean notes document, lots of air, one accent. No quirky concept on top.
