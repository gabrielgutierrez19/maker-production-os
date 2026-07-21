# Three-minute demo script

Target length: 2:50–3:00. Record at a steady pace and keep the cursor movement deliberate.

## 0:00–0:18 — The problem

**Screen:** A representative production spreadsheet, then cut to Shopfloor.

**Voiceover:**

“Shopify knows what a customer bought. It does not know whether their photo is printable, whether the product has been pressed, or which order is late. For many small makers, that physical workflow still lives in a spreadsheet. Shopfloor is the missing production layer.”

## 0:18–0:38 — Orders arrive

**Screen:** Run the five-order simulation command, then refresh the public dashboard.

```bash
curl -X POST 'https://shopfloor-production-os.onrender.com/simulate/orders?n=5'
```

**Voiceover:**

“This uses the real shape of Shopify's `orders/create` webhook. In production, Shopfloor verifies Shopify's signature. For the demo, five synthetic orders arrive with a controlled mix of customer photos.”

## 0:38–1:15 — GPT-5.6 photo quality

**Screen:** Open an On hold photo order. Show the failed photo, plain-language reason, and customer re-upload link.

**Voiceover:**

“Before anything prints, GPT-5.6 evaluates sharpness—especially faces—effective resolution, exposure, and square-crop risk for a 50-millimeter product. It returns structured reasons and a customer-safe explanation. Unknown photos never silently pass if the AI service is unavailable; they remain pending.”

**Screen:** Open the re-upload page, submit a valid replacement, then return to the queue after it passes.

“The customer replaces the image through a secure, expiring, single-use link. The replacement goes back through the same gate and releases the order.”

## 1:15–1:48 — Run production

**Screen:** Advance one order through Printed and Pressed. Open its detail page and timeline.

**Voiceover:**

“The owner taps the physical work forward. Every transition is timestamped. Shopfloor applies business-hour targets, shows the oldest work first, and separates shop-controlled production time from time waiting on the customer or carrier.”

## 1:48–2:18 — Owner operations

**Screen:** Show the top of the Shopfloor owner dashboard, then Datadog's owner dashboard.

**Voiceover:**

“The first question is not server request rate. It is: what needs action now? The owner sees orders and overdue work by stage, then one dominant seven-day production-cycle number. Slow-order, fulfillment, customer-wait, and delivery measures support that answer without competing with it.”

## 2:18–2:39 — Incident copilot

**Screen:** Show a fired Datadog monitor and the Shopfloor incident page, then press play on the voice briefing and let one sentence be heard.

**Voiceover:**

“When Datadog alerts, Shopfloor collects the live queue, oldest order, and recent application events. GPT-5.6 turns that evidence into exactly three sentences: what is happening, the likely cause, and the next action. ElevenLabs then speaks the briefing, so the owner hears the problem without reading a dashboard.”

## 2:39–2:55 — Codex

**Screen:** Show the primary Codex session and a quick scroll through tests or the build log.

**Voiceover:**

“Codex built this with me end to end: architecture, Shopify security, photo recovery, adversarial tests, Datadog, Render deployment, and the redesign after real shop-floor feedback. The primary session stayed continuous so product decisions and code evolved together.”

## 2:55–3:00 — Close

**Screen:** Return to Shopfloor's green owner summary.

**Voiceover:**

“Build the specific, claim the general: Shopfloor starts with one maker and becomes the production operating system for personalized commerce.”

## Recording checklist

- Use synthetic names and photos only
- Hide API keys, browser account menus, email addresses, and Render environment values
- Keep the business name out of every frame
- Show both Codex and GPT-5.6 usage verbally
- Do not claim real Shopify traffic, real customer messages, carrier integration, or outbound phone calls
- Keep the final video under three minutes
