# WhatsApp message templates

The three templates the bot uses outside someone's 24-hour window (see the
"WhatsApp templates" entries in PROJECT_LOG.md, 2026-10-15/16). Submit each
one in **Meta Business Manager -> WhatsApp Manager -> Message templates ->
Create template**, once the real DeKUTCU number is set up.

The **name**, **language** and the **order of the variables** must match
exactly -- the code sends them by name (`app/services/whatsapp_client.py`).
Meta asks for a sample value for each variable; use the samples below.

After all three show **Active**, set `WHATSAPP_TEMPLATES_ENABLED=true` on
Render. Until then the bot sends everything exactly as before.

---

## 1. Message waiting

| Field | Value |
|---|---|
| Name | `dekutcu_message_waiting` |
| Category | Utility |
| Language | English (`en`) |
| Header | none |
| Footer | none |
| Button | Quick reply: `Read it` |

**Body**

```
Hi {{1}}, you have a new message from DeKUTCU about {{2}}. Reply to this message to read it.
```

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | first name (or "there") | Wanjiru |
| `{{2}}` | what it's about | a study guide update |

---

## 2. Fellowship check-in

| Field | Value |
|---|---|
| Name | `dekutcu_checkin` |
| Category | Utility |
| Language | English (`en`) |
| Header | none |
| Footer | none |
| Buttons | Quick reply: `Yes, I was there` and `No, I missed it` |

**Body**

```
Hi {{1}}, were you at {{2}} today? 🙏 Reply YES if you were there, or tell us what kept you away.
```

| Variable | Meaning | Sample |
|---|---|---|
| `{{1}}` | first name (or "there") | Wanjiru |
| `{{2}}` | the activity | Friday Fellowship |

The button words must stay exactly `Yes, I was there` and `No, I missed it`
-- the bot reads a tapped button as those words
(`app/services/fellowship_checkin.py`).

---

## 3. Urgent care alert (to leaders)

| Field | Value |
|---|---|
| Name | `dekutcu_urgent_care` |
| Category | Utility |
| Language | English (`en`) |
| Header | none |
| Footer | none |
| Button | Quick reply: `Show me` |

**Body** (no variables -- the member's name and words are never put in a
template)

```
Urgent: a DeKUTCU member may need support right now. Please reply to this message to see the details and take the case.
```

---

## Things to know when submitting

- **Category.** Meta decides the final category. If it re-labels one as
  Marketing, it costs more (about KES 3.2 instead of about KES 0.52 per
  message in Kenya), and members who opted out of marketing won't receive it.
  If that happens, reword it to be plainly about something the member is
  already part of, and resubmit.
- **Approval time.** Usually minutes, sometimes up to a day. A rejected
  template can be edited and resubmitted.
- **Changing a template later.** Editing an approved template sends it back
  for review. If you change the name or the variables, change
  `app/services/whatsapp_client.py` to match.
